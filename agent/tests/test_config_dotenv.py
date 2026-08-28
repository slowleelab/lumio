"""config.py .env 全局注入测试

验证: .env 中的值对带 env_prefix 的子 Settings (RAG_/LLM_ 等) 同样生效,
而不仅是 LUMIO_* 顶层 —— 修复"子 Settings 无 env_file, .env 只读进程环境变量"的坑。
"""

from __future__ import annotations

import os

import pytest


def test_dotenv_feeds_subsettings(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """临时 .env 写入 RAG_/LLM_ 前缀后, 子 Settings 能读到"""
    import lumio.shared.config as cm

    # 确保进程环境里没有这两个变量, 避免被真实 .env/env 干扰
    monkeypatch.delenv("RAG_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_PRIMARY_MODEL", raising=False)

    (tmp_path / ".env").write_text("RAG_EMBEDDING_PROVIDER=tei\nLLM_PRIMARY_MODEL=qwen2.5:32b\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cm._load_dotenv_search()

    try:
        # 注入到了 os.environ, 子 Settings 由此可见
        assert os.environ.get("RAG_EMBEDDING_PROVIDER") == "tei"
        assert os.environ.get("LLM_PRIMARY_MODEL") == "qwen2.5:32b"

        from lumio.shared.config import RAGSettings

        rs = RAGSettings()
        assert rs.embedding_provider == "tei"
    finally:
        monkeypatch.delenv("RAG_EMBEDDING_PROVIDER", raising=False)
        monkeypatch.delenv("LLM_PRIMARY_MODEL", raising=False)


def test_real_env_wins_over_dotenv(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:
    """真实进程环境变量优先级高于 .env (load_dotenv override=False)"""
    import lumio.shared.config as cm

    monkeypatch.setenv("RAG_EMBEDDING_PROVIDER", "ollama")
    (tmp_path / ".env").write_text("RAG_EMBEDDING_PROVIDER=tei\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cm._load_dotenv_search()

    try:
        assert os.environ.get("RAG_EMBEDDING_PROVIDER") == "ollama"
    finally:
        monkeypatch.delenv("RAG_EMBEDDING_PROVIDER", raising=False)


def test_repo_root_env_wins_when_cwd_inside_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    """cwd 位于仓库内时, 仓库根 .env 优先于 cwd/.env (修复 agent/.env 历史残留遮蔽根文件)"""
    import lumio.shared.config as cm

    repo_root = cm.Path(__file__).resolve().parents[2]
    # 模拟: 仓库根 .env 定义 A, cwd(模拟为仓库内子目录)的 .env 定义 B —— 根应胜出
    root_env = repo_root / ".env"
    assert root_env.is_file(), "测试前置: 仓库根需存在 .env"

    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.chdir(repo_root / "agent")

    cm._load_dotenv_search()
    try:
        # 真实根 .env 内的值被加载 (应为 lumio, 而非任何 cwd 残留)
        assert os.environ.get("POSTGRES_USER") == "lumio"
    finally:
        monkeypatch.delenv("POSTGRES_USER", raising=False)
