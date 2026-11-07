//! Small, allowlisted hardware facts; no inventory commands, serials or UUIDs.
use serde::Serialize;
use std::{collections::BTreeSet, fs};

#[derive(Debug, Default, Serialize)]
pub struct Specs {
    pub machine: Option<String>,
    pub cpu_model: Option<String>,
    pub cores: Option<usize>,
    pub threads: Option<usize>,
}

fn text(value: &str) -> Option<String> {
    let value = value.trim();
    (!value.is_empty() && value.len() <= 256).then(|| value.to_owned())
}

fn parse(cpuinfo: &str, machine: &str) -> Specs {
    let mut specs = Specs {
        machine: text(machine),
        ..Default::default()
    };
    let mut processors = BTreeSet::new();
    let mut cores = BTreeSet::new();
    let mut complete_topology = true;
    for block in cpuinfo.split("\n\n") {
        let field = |key: &str| {
            block
                .lines()
                .filter_map(|line| line.split_once(':'))
                .find_map(|(name, value)| (name.trim() == key).then_some(value.trim()))
        };
        if specs.cpu_model.is_none() {
            specs.cpu_model = field("model name").and_then(text);
        }
        if let Some(processor) = field("processor").and_then(|s| s.parse::<u32>().ok()) {
            processors.insert(processor);
            match (
                field("physical id").and_then(|s| s.parse::<u32>().ok()),
                field("core id").and_then(|s| s.parse::<u32>().ok()),
            ) {
                (Some(socket), Some(core)) => {
                    cores.insert((socket, core));
                }
                _ => complete_topology = false,
            }
        }
    }
    specs.threads = (!processors.is_empty()).then_some(processors.len());
    specs.cores = (complete_topology && !cores.is_empty()).then_some(cores.len());
    specs
}

pub fn collect() -> Specs {
    parse(
        &fs::read_to_string("/proc/cpuinfo").unwrap_or_default(),
        &fs::read_to_string("/sys/class/dmi/id/product_name").unwrap_or_default(),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn counts_physical_cores_across_sockets_and_logical_threads() {
        let cpuinfo = [(0, 0, 0), (1, 0, 0), (2, 0, 1), (3, 1, 0)].into_iter()
            .map(|(processor, socket, core)| format!("processor : {processor}\nmodel name : Example CPU\nphysical id : {socket}\ncore id : {core}\nserial : private\n\n"))
            .collect::<String>();
        let specs = parse(&cpuinfo, "  Example PC\n");
        assert_eq!(specs.machine.as_deref(), Some("Example PC"));
        assert_eq!(specs.cpu_model.as_deref(), Some("Example CPU"));
        assert_eq!(specs.cores, Some(3));
        assert_eq!(specs.threads, Some(4));
        assert!(!serde_json::to_string(&specs).unwrap().contains("private"));
    }

    #[test]
    fn missing_topology_is_unknown_not_an_invented_core_count() {
        let specs = parse(
            "processor : 0\nmodel name : CPU\n\nprocessor : 1\nphysical id : 0\ncore id : 0\n",
            "",
        );
        assert_eq!(specs.threads, Some(2));
        assert!(specs.cores.is_none());
        assert!(specs.machine.is_none());
        assert!(parse("", "").threads.is_none());
        assert!(text(&"x".repeat(257)).is_none());
    }
}
