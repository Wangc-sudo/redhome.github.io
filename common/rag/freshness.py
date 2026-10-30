"""Best-effort failure status for index CLI exits before normal publishing."""


def record_run_failure(index_keys, *, source_type, source_id, reason,
                       embedding_model=None, dim=None):
    try:
        from common.public_data import db
        from common.public_data.freshness_repository import (
            KnowledgeIndexFreshnessRepository,
        )
        from common.public_data.settings import Settings

        settings = Settings.from_environment()
        connection = db.connect(settings.mart_database)
        try:
            repository = KnowledgeIndexFreshnessRepository(connection)
            for index_key in index_keys:
                repository.record({
                    "index_key": index_key,
                    "source_type": source_type,
                    "source_id": str(source_id)[:128],
                    "embedding_model": embedding_model,
                    "dim": dim,
                    "status": "degraded",
                    "last_error": reason,
                })
        finally:
            connection.close()
    except Exception:
        return False
    return True
