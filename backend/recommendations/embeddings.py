import numpy as np
from fastembed import TextEmbedding

embedding_model = TextEmbedding(
    model_name="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )


def text_to_vector(text: str) -> np.ndarray:
    vector = next(iter(embedding_model.embed([text])), None)
    if vector is None:
        raise ValueError(f"Model {embedding_model.model_name} return no embeddings")
    return vector
