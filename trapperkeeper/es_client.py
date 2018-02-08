from elasticsearch import Elasticsearch


def get_elasticsearch_client(host):
    return Elasticsearch(host)


def index_trap_to_elasticsearch(es, index_name, doc):
    return es.index(index=index_name, doc_type=index_name, body=doc)