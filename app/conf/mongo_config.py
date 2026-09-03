from dataclasses import dataclass
import os

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class MongoConfig:
    mongo_url: str | None
    database_name: str | None
    meeting_collection: str
    profile_attribute_collection: str
    server_selection_timeout_ms: int
    connect_timeout_ms: int
    max_pool_size: int


mongo_config = MongoConfig(
    mongo_url=os.getenv("MONGO_URL"),
    database_name=os.getenv("MONGO_DB_NAME"),
    meeting_collection=os.getenv(
        "MONGO_MEETING_MEMORY_COLLECTION",
        "meeting_episodes",
    ),
    profile_attribute_collection=os.getenv(
        "MONGO_PROFILE_ATTRIBUTE_COLLECTION",
        "profile_attributes",
    ),
    server_selection_timeout_ms=int(
        os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000")
    ),
    connect_timeout_ms=int(
        os.getenv("MONGO_CONNECT_TIMEOUT_MS", "5000")
    ),
    max_pool_size=int(os.getenv("MONGO_MAX_POOL_SIZE", "20")),
)
