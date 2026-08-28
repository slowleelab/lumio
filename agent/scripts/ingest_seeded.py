"""摄入已 seed 的 PENDING 文档

seed_knowledge.py 只创建 KbDocument 记录 (status=PENDING) 并上传到 MinIO，
本脚本把这些文档真正摄入 ES/Milvus（Parse→Clean→Chunk→Embed→Dual-Write）。

使用方式（需先安装好 embedding 模型且 make dev/middleware 已启动）:
    cd agent && PYTHONPATH=. poetry run python scripts/ingest_seeded.py

可选参数:
    --limit N    只摄入前 N 条（默认全部）
    --status     只见打印待摄入列表，不动手
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_ASYNC_IMPORTS = None


def _connect_es(settings):
    from elasticsearch import AsyncElasticsearch

    es_kwargs: dict = {"hosts": [settings.elasticsearch.hosts]}
    if settings.elasticsearch.username:
        es_kwargs["basic_auth"] = (settings.elasticsearch.username, settings.elasticsearch.password)
    es_kwargs["verify_certs"] = settings.elasticsearch.verify_certs
    return AsyncElasticsearch(**es_kwargs)


def _connect_milvus(settings):
    from pymilvus import Collection, connections

    connections.connect(alias="default", host=settings.milvus.host, port=settings.milvus.port, timeout=3)
    collection = Collection(settings.milvus.collection_name)
    collection.load()
    return collection


def _embedding_provider(settings):
    from lumio.services.common.embedding import create_embedding_provider

    ollama_base = settings.llm.base_url.replace("/v1", "").rstrip("/")
    return create_embedding_provider(
        provider_type=settings.rag.embedding_provider,
        ollama_base_url=ollama_base,
        ollama_model=settings.rag.embedding_model,
        tei_base_url=settings.rag.tei_base_url,
        tei_model=settings.rag.embedding_model,
        dim=settings.rag.embedding_dim,
        batch_size=settings.rag.embedding_batch_size,
        timeout=settings.rag.embedding_timeout,
        max_retries=settings.rag.embedding_max_retries,
    )


def _fetch_minio_text(settings, object_key: str) -> str:
    from minio import Minio

    client = Minio(
        settings.minio.endpoint,
        access_key=settings.minio.access_key,
        secret_key=settings.minio.secret_key,
        secure=False,
    )
    resp = client.get_object(settings.minio.bucket, object_key)
    try:
        return resp.read().decode("utf-8")
    finally:
        resp.close()
        resp.release_conn()


async def run(limit: int | None, dry_run: bool) -> None:
    from lumio.shared.config import get_settings
    from lumio.shared.orm_models import KbDocStatus, KbDocument, KbSourceType

    settings = get_settings()

    # 1. 查 PENDING 文档
    sync_engine = create_engine(settings.database.sync_dsn)
    with Session(sync_engine) as session:
        docs = (
            session.query(KbDocument)
            .filter(KbDocument.status == KbDocStatus.PENDING, KbDocument.is_deleted.is_(False))
            .order_by(KbDocument.created_at)
            .limit(limit)
            .all()
        )

    if not docs:
        print("没有待摄入的 PENDING 文档。")
        return

    print(f"待摄入 {len(docs)} 条文档: " + ", ".join(d.title for d in docs))

    if dry_run:
        print("--status 模式，跳过摄入。")
        return

    # 2. 组装下游依赖

    from lumio.services.common.ingestion import ingest_document
    from lumio.shared.models import DocumentMetadata

    embedding_provider = _embedding_provider(settings)
    es_client = _connect_es(settings)
    milvus_collection = _connect_milvus(settings)

    from lumio.services.common.database import init_global_session_factory

    factory = init_global_session_factory()
    async with factory() as db:
        for doc in docs:
            try:
                text = _fetch_minio_text(settings, doc.file_path)
            except Exception as e:
                print(f"  ❌ {doc.title}: 读取 MinIO 失败 {e}")
                continue

            metadata = DocumentMetadata(
                doc_id=str(doc.id),
                category=doc.category,
                doc_type=doc.doc_type or "faq",
                keywords=[],
                card_type=doc.card_type,
                customer_tier=doc.customer_tier,
                effective_date=doc.effective_date.isoformat() if doc.effective_date else None,
                expiry_date=doc.expiry_date.isoformat() if doc.expiry_date else None,
                security_level=doc.security_level,
                version=doc.version,
            )

            try:
                final_status = await ingest_document(
                    doc_id=doc.id,
                    file_path=text,
                    source_type=KbSourceType.MARKDOWN,
                    metadata=metadata,
                    embedding_provider=embedding_provider,
                    db_session=db,
                    es_client=es_client,
                    milvus_collection=milvus_collection,
                )
                # ingest_document 已把 doc.status 置为 COMPLETED/FAILED 并 flush，
                # 由外层 async with 退出时统一 commit（勿再另开同步会话更新，避免行锁自锁）
                print(f"  ✅ {doc.title}: {final_status}")
            except Exception:
                logger.exception("文档摄入异常: %s", doc.title)
                await db.rollback()

    await es_client.close()


def main():
    parser = argparse.ArgumentParser(description="摄入 seed 的 PENDING 文档")
    parser.add_argument("--limit", type=int, default=None, help="只摄入前 N 条")
    parser.add_argument("--status", action="store_true", help="只列出待摄入文档，不摄入")
    args = parser.parse_args()

    import asyncio

    asyncio.run(run(args.limit, args.status))


if __name__ == "__main__":
    sys.exit(main())
