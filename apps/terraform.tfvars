cluster = {
  name = "default"
  config_path = "../../../.kube/config"
  host = "https://mathijs.se:6443"
  hosts = [
    "www.mathijs.se",
    "mathijs.se",
    "192.168.68.113.nip.io"
  ]
}

per_environment_configuration = [
  {
    name = "production"
    applications = []
  }
]

ingresses = [
  {
    name = "mathijs.se"
    namespace = "mathijsse"
  }
]

databases = [
#   {
#     type = "postgresql",
#     namespace = "homelab",
#     name = "keycloak-db"
#   }
]

externals = [
    {
      name = "ingress-nginx"
      repo = "https://kubernetes.github.io/ingress-nginx"
      version = "4.12.3"
    },
    {
      name = "hajimari"
      repo = "https://hajimari.io"
      version = "1.2.0"
      namespace = "homelab"
    },
    # {
    #  name = "postgresql"
    #  repo = "https://charts.bitnami.com/bitnami"
    #  version = "13.1.0"
    #  namespace = "homelab"
    # },
    {
      name = "pihole",
      repo = "https://mojo2600.github.io/pihole-kubernetes/",
      version = "2.11.0",
      namespace = "homelab"
    },
]
