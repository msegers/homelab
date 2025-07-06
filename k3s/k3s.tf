# k3s install will give warnings if memory group is not enabled
resource "ssh_resource" "configure_memory_group" {
  for_each = local.servers
  host     = each.value.host
  user     = each.value.user
  commands = ["sed -i '1s/$/ cgroup_memory=1 cgroup_enable=memory/' /boot/firmware/cmdline.txt"]
  private_key = file(each.value.private_key)
}

# Install docker
# TODO: when running this fresh, consider checking if apt-get install docker-ce works
resource "ssh_resource" "install_docker" {
  for_each = local.servers
  host     = each.value.host
  user     = each.value.user
  commands = [ "curl -sSL https://get.docker.com/ | sudo sh" ]
  private_key = file(each.value.private_key)
  timeout = "10m"
  depends_on = [ssh_resource.configure_memory_group]
}

# install k3s
resource "ssh_resource" "install_k3s" {
  for_each = local.servers
  host     = each.value.host
  user     = each.value.user
  commands = [
    "curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION='v1.33.0+k3s1' sh -s - server --docker --write-kubeconfig-mode 644 --disable=traefik --tls-san=${each.value.host} --bind-address=${each.value.host} --advertise-address=${each.value.host} --node-ip=${each.value.host}"
  ]
  private_key = file(each.value.private_key)
  # shouldn't take this long unless you have slow internets :-)
  timeout = "10m"
  depends_on = [ssh_resource.install_docker]
}

# Replaces local ip into the main node's IP in case multiple raspi's
resource "ssh_resource" "kubeconfig" {
  for_each = local.servers
  host     = each.value.host
  user     = each.value.user

  commands = ["sed -i -e \"s/127.0.0.1/${local.servers[0].host}/g\" /etc/rancher/k3s/k3s.yaml "]
  private_key = file(each.value.private_key)

   depends_on = [ssh_resource.install_k3s, ssh_resource.install_docker]
}

# for destroy, we remove k3s
resource "ssh_resource" "uninstall_k3s" {
  for_each    = { for server in var.servers : server.host => server }
  host        = each.value.host
  when        = "destroy"
  user        = each.value.user
  commands    = ["bash -c 'k3s-killall.sh; k3s-uninstall.sh;'"]
  private_key = file(each.value.private_key)
}
