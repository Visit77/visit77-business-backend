from django.core.files.storage import storages


def get_private_document_storage():
    """Resolve the configured private backend for the current environment."""
    return storages["private"]
