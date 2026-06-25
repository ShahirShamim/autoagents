
output "data_store_id" {
  description = "Data store ID"
  value       = data.external.data_store_id.result.data_store_id
}

output "search_engine_id" {
  description = "Search engine ID"
  value       = google_discovery_engine_search_engine.search_engine.engine_id
}

output "docs_bucket_name" {
  description = "Document bucket name"
  value       = google_storage_bucket.docs_bucket.name
}

