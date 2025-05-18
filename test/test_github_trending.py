import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))  # プロジェクトルートを検索パスに追加
import datetime
import pytest
from unittest.mock import patch, MagicMock

from nook.functions.github_trending.github_trending import GithubTrending, Config, Repository, lambda_handler

# --- テスト用のサンプル HTML ---
HTML_SAMPLE = """
<html>
  <body>
    <div>
      <h2 class="h3 lh-condensed">
        <a href="/owner/repo">owner/repo</a>
      </h2>
      <p class="col-9 color-fg-muted my-1 pr-4">A sample description</p>
      <a href="/owner/repo/stargazers">1,234</a>
    </div>
  </body>
</html>
"""

# --- _retrieve_repositories の単体テスト ---
def test_retrieve_repositories_parses_html():
    mock_resp = MagicMock()
    mock_resp.text = HTML_SAMPLE

    with patch("requests.get", return_value=mock_resp):
        gt = GithubTrending()
        repos = gt._retrieve_repositories("https://example.com/trending")

    assert isinstance(repos, list)
    assert len(repos) == 1

    repo = repos[0]
    assert isinstance(repo, Repository)
    assert repo.name == "owner/repo"
    assert repo.link == "https://github.com/owner/repo"
    assert repo.description == "A sample description"
    assert repo.stars == 1234

# --- __call__ が正しく Markdown ファイルを書き出すかをテスト ---
def test_call_writes_markdown(tmp_path, monkeypatch):
    # 出力先を tmp_path にリダイレクト
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path))

    # load_languages を固定
    monkeypatch.setattr(Config, "load_languages", classmethod(lambda cls: ["python"]))

    # requests.get は常に同じ HTML_SAMPLE を返す
    mock_resp = MagicMock()
    mock_resp.text = HTML_SAMPLE
    monkeypatch.setattr("requests.get", lambda url: mock_resp)

    # 実行
    gt = GithubTrending()
    gt()

    # 出力ファイルのパスを組み立て
    today = datetime.date.today().strftime("%Y-%m-%d")
    out_file = tmp_path / f"github_trending/{today}.md"

    # ファイルが作られていること
    assert out_file.exists(), f"{out_file} が存在しません"

    content = out_file.read_text(encoding="utf-8")

    # Daily Trends と Python Trends のヘッダーが含まれていること
    assert "# Daily Trends" in content
    assert "# Python Trends" in content

    # サンプルリポジトリの情報が Markdown 内に現れていること
    assert "owner/repo" in content
    assert "**Score**: 1234" in content
    assert "A sample description" in content

# --- lambda_handler のテスト ---
def test_lambda_handler_calls_github_trending(monkeypatch):
    # GithubTrending.__call__ が呼ばれたか捕捉するフラグ
    called = {"flag": False}
    def fake_call(self):
        called["flag"] = True

    monkeypatch.setattr(GithubTrending, "__call__", fake_call)

    # event.source が aws.events のとき
    result = lambda_handler({"source": "aws.events"}, None)
    assert result == {"statusCode": 200}
    assert called["flag"], "__call__ が呼ばれていません"

def test_lambda_handler_non_event(monkeypatch):
    # __call__ が呼ばれると例外を投げるようにして、呼ばれないことをチェック
    def fake_call(self):
        raise AssertionError("呼ばれてはいけません")
    monkeypatch.setattr(GithubTrending, "__call__", fake_call)

    result = lambda_handler({"source": "not.aws.events"}, None)
    assert result == {"statusCode": 200}
