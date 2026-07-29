# docker-bake.hcl — build definition consumed by `docker buildx bake` and by the
# org's Diatreme release action. Its PRESENCE is what makes Diatreme build the
# image MULTI-ARCH; with only a Dockerfile it builds amd64-only, which then
# ImagePullBackOffs on the arm64 native-cloud workers (topology.sargeant.co/tier).
#
#   Local single-arch build+load:   docker buildx bake app-local
#   Multi-arch build (CI/release):  docker buildx bake app
#   Override the version:           VERSION=1.2.3 docker buildx bake app

variable "VERSION" { default = "latest" }
variable "REGISTRY" { default = "ghcr.io" }
variable "IMAGE_NAME" { default = "magmamoose/github-timesheet" }
variable "PLATFORMS" { default = "linux/amd64,linux/arm64" }

group "default" {
  targets = ["app"]
}

target "app" {
  context    = "."
  dockerfile = "Dockerfile"
  target     = "runtime"
  platforms  = split(",", PLATFORMS)
  tags = [
    "${REGISTRY}/${IMAGE_NAME}:${VERSION}",
  ]
}

# Convenience target for local dev: single-arch so it can `--load` into the local
# Docker engine (multi-arch manifests can't be loaded, only pushed).
target "app-local" {
  inherits  = ["app"]
  platforms = ["linux/amd64"]
  tags      = ["${IMAGE_NAME}:dev"]
}
