//! Minimal, bounded Steam A2S queries. Unsupported/split packets fail open:
//! never infer an empty server from a response we cannot fully understand.
use anyhow::{Result, bail, ensure};
use std::{
    net::{SocketAddr, UdpSocket},
    time::Duration,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Observation {
    pub info_players: u8,
    pub player_list_entries: u8,
}

impl Observation {
    pub fn count(self) -> Result<u8> {
        ensure!(
            self.info_players == self.player_list_entries,
            "Player query responses disagree"
        );
        Ok(self.info_players)
    }
}

fn exchange(address: SocketAddr, request: &[u8]) -> Result<Vec<u8>> {
    let socket = UdpSocket::bind(if address.is_ipv4() {
        "0.0.0.0:0"
    } else {
        "[::]:0"
    })?;
    socket.set_read_timeout(Some(Duration::from_secs(3)))?;
    socket.connect(address)?;
    let mut outgoing = request.to_vec();
    let mut buffer = [0u8; 65536];
    for _ in 0..3 {
        socket.send(&outgoing)?;
        let size = socket.recv(&mut buffer)?;
        ensure!(
            size >= 5 && buffer[..4] == [255; 4],
            "Unsupported/split A2S response"
        );
        if buffer[4] != 0x41 {
            return Ok(buffer[4..size].to_vec());
        }
        ensure!(size == 9, "Malformed A2S challenge");
        outgoing = request.to_vec();
        if request[4] == 0x55 {
            outgoing.truncate(5);
        }
        outgoing.extend_from_slice(&buffer[5..9]);
    }
    bail!("Repeated A2S challenges")
}

struct Packet<'a>(&'a [u8]);
impl Packet<'_> {
    fn take(&mut self, count: usize) -> Result<&[u8]> {
        ensure!(self.0.len() >= count, "Truncated A2S packet");
        let (head, tail) = self.0.split_at(count);
        self.0 = tail;
        Ok(head)
    }
    fn byte(&mut self) -> Result<u8> {
        Ok(self.take(1)?[0])
    }
    fn string(&mut self) -> Result<()> {
        let length = self
            .0
            .iter()
            .position(|b| *b == 0)
            .ok_or_else(|| anyhow::anyhow!("Unterminated A2S string"))?;
        self.take(length + 1)?;
        Ok(())
    }
}

fn info_count(bytes: &[u8]) -> Result<u8> {
    let mut packet = Packet(bytes);
    ensure!(packet.byte()? == 0x49, "Unexpected A2S_INFO type");
    packet.byte()?; // protocol
    for _ in 0..4 {
        packet.string()?;
    }
    packet.take(2)?; // app ID
    let players = packet.byte()?;
    packet.take(6)?; // max players, bots, server type, environment, visibility, VAC
    packet.string()?; // version; optional EDF fields may follow
    Ok(players)
}

fn listed_count(bytes: &[u8]) -> Result<u8> {
    let mut packet = Packet(bytes);
    ensure!(packet.byte()? == 0x44, "Unexpected A2S_PLAYER type");
    let count = packet.byte()?;
    for _ in 0..count {
        packet.byte()?; // index
        packet.string()?; // blank names still count
        packet.take(8)?; // score and duration
    }
    ensure!(packet.0.is_empty(), "Trailing A2S_PLAYER data");
    Ok(count)
}

pub fn query(address: SocketAddr) -> Result<Observation> {
    let info = exchange(address, b"\xff\xff\xff\xffTSource Engine Query\0")?;
    let players = exchange(address, b"\xff\xff\xff\xffU\xff\xff\xff\xff")?;
    Ok(Observation {
        info_players: info_count(&info)?,
        player_list_entries: listed_count(&players)?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn blank_names_count_and_truncation_is_rejected() {
        let mut bytes = vec![0x44, 1, 0, 0];
        bytes.extend([0; 8]);
        assert_eq!(listed_count(&bytes).unwrap(), 1);
        bytes.pop();
        assert!(listed_count(&bytes).is_err());
        assert!(listed_count(&[0x44, 0, 1]).is_err());
        assert!(
            Observation {
                info_players: 0,
                player_list_entries: 1
            }
            .count()
            .is_err()
        );
    }

    #[test]
    fn info_parses_counts_and_rejects_malformed_responses() {
        let mut bytes = vec![0x49, 17];
        bytes.extend(b"name\0map\0folder\0game\0");
        bytes.extend([0, 0, 2, 10, 0, b'd', b'l', 0, 1]);
        bytes.extend(b"version\0");
        assert_eq!(info_count(&bytes).unwrap(), 2);
        bytes.pop();
        assert!(info_count(&bytes).is_err());
        assert!(info_count(&[]).is_err());
    }

    #[test]
    fn challenge_exchange_over_real_udp() {
        let socket = UdpSocket::bind("127.0.0.1:0").unwrap();
        socket
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let address = socket.local_addr().unwrap();
        let task = std::thread::spawn(move || {
            let mut bytes = [0; 100];
            let (_, client) = socket.recv_from(&mut bytes).unwrap();
            socket.send_to(b"\xff\xff\xff\xffAabcd", client).unwrap();
            let (length, client) = socket.recv_from(&mut bytes).unwrap();
            assert_eq!(&bytes[..length], b"\xff\xff\xff\xffUabcd");
            socket.send_to(b"\xff\xff\xff\xffD\0", client).unwrap();
        });
        assert_eq!(
            exchange(address, b"\xff\xff\xff\xffU\xff\xff\xff\xff").unwrap(),
            b"D\0"
        );
        task.join().unwrap();
    }
}
