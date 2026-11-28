resource "kind_cluster" "pilot" {
  lifecycle {
    prevent_destroy = true
  }
}
