import logging
from collections import defaultdict
from functools import lru_cache

from django.conf import settings
from pymongo import MongoClient
from pymongo.errors import PyMongoError


@lru_cache(maxsize=1)
def _category_collection(uri):
    client = MongoClient(uri, timeoutMS=3000)
    return client.get_default_database()["category_mongo"]


def category_languages(category_ids):
    uri = settings.CATEGORY_MONGODB_URI
    if not uri or not category_ids:
        return {}

    languages = defaultdict(list)
    fields = ("category_id", "name", "lang", "lang_code", "rank", "is_active")
    try:
        records = _category_collection(uri).find(
            {"category_id": {"$in": sorted(category_ids)}},
            {field: 1 for field in fields},
        ).sort("_id")
        for record in records:
            languages[record["category_id"]].append({
                "id": str(record["_id"]),
                **{field: record.get(field) for field in fields},
            })
    except (PyMongoError, ValueError):
        logging.getLogger(__name__).warning("Could not read coupon category translations from MongoDB.")
        return {}
    return languages
