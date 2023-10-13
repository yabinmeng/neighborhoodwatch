import pytest

from neighborhoodwatch.generate_dataset import generate_base_dataset, get_embeddings_from_map

from neighborhoodwatch.generate_dataset import generate_query_dataset


def test_generate_query_dataset():
    generate_query_dataset(10000)

def test_generate_base_dataset():
    generate_base_dataset('query_vector_data_1000.parquet',1000)

def test_get_embeddings_from_map():
    response = get_embeddings_from_map([(0,['hi there', 'how are you']),(1,['Im good', 'and you', 'sup'])])
    print(response)