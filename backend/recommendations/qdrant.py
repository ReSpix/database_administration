import os

from pydantic import BaseModel, ConfigDict
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = "user_profiles"
VECTOR_SIZE = 384

client = QdrantClient(QDRANT_URL)


def init_qdrant():
    if not client.collection_exists(COLLECTION_NAME):
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_SIZE, distance=Distance.COSINE),
        )
        print(f"Collection '{COLLECTION_NAME}' created.")


class VectorItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vector: list[float] | None = None
    payload: dict | None = None
    score: float | None = None


class SearchQuery(BaseModel):
    vector: list[float]
    limit: int = 5


def upsert_point(item: VectorItem):
    if len(item.vector) != VECTOR_SIZE:
        raise ValueError(f"Vector size must be {VECTOR_SIZE}")

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(id=item.id, vector=item.vector, payload=item.payload)],
    )


def search_similar(query: SearchQuery):
    if len(query.vector) != VECTOR_SIZE:
        raise ValueError(f"Query vector size must be {VECTOR_SIZE}")

    hits = client.query_points(
        collection_name=COLLECTION_NAME, query=query.vector, limit=query.limit
    )

    return [VectorItem.model_validate(hit) for hit in hits.points]
