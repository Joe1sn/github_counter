"""
GitHub 仓库统计脚本

统计指定 GitHub 仓库在某个时间段内的：
  - Star 数量（新增）
  - Commit 数量
  - PR 数量（新增）
  - Fork 数量（新增）
  - Contributor 数量（时间段内活跃贡献者）
  - Watch 数量（当前总数，API 不支持按时间查询）

支持 SOCKS5 代理。
"""

import argparse
import os
import sys
from datetime import datetime, timezone, timedelta

import requests


# ---------------------------------------------------------------------------
# 代理会话
# ---------------------------------------------------------------------------

def build_session(socks5_proxy: str | None = None) -> requests.Session:
    """创建 requests.Session，可选 SOCKS5 代理。"""
    session = requests.Session()
    session.headers.update({
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "github-stats-script/1.0",
    })
    if socks5_proxy:
        # 格式: socks5://user:pass@host:port 或 socks5://host:port
        session.proxies.update({
            "http": socks5_proxy,
            "https": socks5_proxy,
        })
    return session


# ---------------------------------------------------------------------------
# GitHub API 基础调用
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"


def api_get(session: requests.Session, url: str, params: dict | None = None,
            token: str | None = None,
            extra_headers: dict[str, str] | None = None) -> requests.Response:
    """发起 GET 请求，自动处理认证。"""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    resp = session.get(url, params=params, headers=headers or None, timeout=30)
    if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
        reset_ts = int(resp.headers.get("X-RateLimit-Reset", 0))
        reset_time = datetime.fromtimestamp(reset_ts, tz=timezone.utc)
        print(f"[!] API 速率限制已耗尽，重置时间: {reset_time.isoformat()}", file=sys.stderr)
    resp.raise_for_status()
    return resp


def paginate(session: requests.Session, url: str, params: dict | None = None,
             token: str | None = None,
             extra_headers: dict[str, str] | None = None) -> list[dict]:
    """自动处理分页，返回所有页的数据。"""
    results: list[dict] = []
    page = 1
    while True:
        p = {"per_page": 100, "page": page}
        if params:
            p.update(params)
        resp = api_get(session, url, params=p, token=token,
                       extra_headers=extra_headers)
        data = resp.json()
        if not data:
            break
        results.extend(data)
        link = resp.headers.get("Link", "")
        if 'rel="next"' not in link:
            break
        page += 1
    return results


# ---------------------------------------------------------------------------
# 统计数据
# ---------------------------------------------------------------------------

def count_stars(session: requests.Session, owner: str, repo: str,
                since: datetime, until: datetime | None = None,
                token: str | None = None) -> int:
    """统计时间段内新增的 star 数量。"""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/stargazers"
    extra_headers = {
        "Accept": "application/vnd.github.v3.star+json",
    }

    all_data = paginate(session, url, token=token, extra_headers=extra_headers)

    # 检查响应中是否包含 starred_at（验证自定义 media type 生效）
    if all_data and all("starred_at" not in item for item in all_data[:10]):
        print("[!] 警告: Stargazers 响应中没有 'starred_at' 字段，"
              "请确认 Accept header 是否正确", file=sys.stderr)

    count = 0
    for entry in all_data:
        starred_at_str = entry.get("starred_at")
        if not starred_at_str:
            continue
        starred_at = datetime.fromisoformat(
            starred_at_str.replace("Z", "+00:00")
        )
        if starred_at < since:
            continue
        if until and starred_at > until:
            continue
        count += 1
    return count


def fetch_commits(session: requests.Session, owner: str, repo: str,
                  since: datetime, until: datetime | None = None,
                  token: str | None = None) -> list[dict]:
    """获取时间段内的所有 commit 原始数据。"""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/commits"
    params: dict = {
        "per_page": 100,
        "since": since.isoformat(),
    }
    if until:
        params["until"] = until.isoformat()

    results = paginate(session, url, params=params, token=token)

    # API 可能在分页边界返回范围外的数据，客户端再做一次过滤
    filtered: list[dict] = []
    for commit in results:
        date_str = (commit.get("commit", {})
                    .get("committer", {})
                    .get("date"))
        if not date_str:
            continue
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt < since:
            continue
        if until and dt > until:
            continue
        filtered.append(commit)
    return filtered


def count_commits(session: requests.Session, owner: str, repo: str,
                  since: datetime, until: datetime | None = None,
                  token: str | None = None) -> int:
    """统计时间段内的 commit 数量。"""
    return len(fetch_commits(session, owner, repo, since, until, token))


def count_unique_contributors(
    commits: list[dict],
) -> int:
    """从 commit 数据中统计唯一贡献者数量。"""
    authors: set[str] = set()
    for commit in commits:
        author = commit.get("author")
        if author and isinstance(author, dict) and author.get("login"):
            authors.add(author["login"])
        else:
            # 兜底：用 commit author name
            name = commit.get("commit", {}).get("author", {}).get("name")
            if name:
                authors.add(f"name:{name}")
    return len(authors)


def count_forks(session: requests.Session, owner: str, repo: str,
                since: datetime, until: datetime | None = None,
                token: str | None = None) -> int:
    """统计时间段内新增的 fork 数量。

    Forks API 返回的每个 fork 带有 created_at，按 newest 排序，
    遍历到超出时间范围为止。
    """
    url = f"{GITHUB_API}/repos/{owner}/{repo}/forks"
    all_data = paginate(session, url, params={"sort": "newest"}, token=token)

    count = 0
    for entry in all_data:
        created_str = entry.get("created_at")
        if not created_str:
            continue
        created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created_at < since:
            break  # 按 newest 排序，遇到超出的直接结束
        if until and created_at > until:
            continue
        count += 1
    return count


def fetch_repo_info(session: requests.Session, owner: str, repo: str,
                    token: str | None = None) -> dict:
    """获取仓库当前基础信息。"""
    url = f"{GITHUB_API}/repos/{owner}/{repo}"
    resp = api_get(session, url, token=token)
    data = resp.json()
    return {
        "subscribers_count": data.get("subscribers_count", 0),
        "forks_count": data.get("forks_count", 0),
        "stargazers_count": data.get("stargazers_count", 0),
    }


def count_prs(session: requests.Session, owner: str, repo: str,
              since: datetime, until: datetime | None = None,
              token: str | None = None) -> int:
    """统计时间段内创建的 PR 数量。"""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls"
    params: dict = {
        "state": "all",
        "per_page": 100,
        "sort": "created",
        "direction": "desc",
    }

    results = paginate(session, url, params=params, token=token)
    count = 0
    for pr in results:
        created_str = pr.get("created_at")
        if not created_str:
            continue
        created_at = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        if created_at < since:
            continue
        if until and created_at > until:
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_time(s: str) -> datetime:
    """解析用户输入的时间字符串。

    支持格式:
      - 2025-01-01
      - 2025-01-01T00:00:00
      - N_days  / N_d  （相对于当前时间，例如 7_days）
      - N_weeks / N_w
      - N_months
    """
    s = s.strip()

    # 相对时间
    for suffix, kw in [("_days", "days"), ("_d", "days"),
                        ("_weeks", "weeks"), ("_w", "weeks"),
                        ("_months", "months")]:
        if s.endswith(suffix):
            try:
                n = int(s[: -len(suffix)])
            except ValueError:
                break
            now = datetime.now(timezone.utc)
            if kw == "days":
                return now - timedelta(days=n)
            elif kw == "weeks":
                return now - timedelta(weeks=n)
            elif kw == "months":
                return now - timedelta(days=n * 30)

    # 绝对时间
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

    raise ValueError(f"无法解析时间: {s}")


def main():
    parser = argparse.ArgumentParser(
        description="统计 GitHub 仓库在时间段内的 Star / Commit / PR 数量",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "时间格式示例:\n"
            "  2025-01-01          绝对日期\n"
            "  2025-01-01T00:00:00 绝对时间\n"
            "  30_days / 30_d      最近 30 天\n"
            "  12_weeks / 12_w     最近 12 周\n"
            "  6_months            最近 6 个月\n"
        ),
    )
    parser.add_argument("repo", help="GitHub 仓库, 格式 owner/repo (例如 torvalds/linux)")
    parser.add_argument("since", help="起始时间（或相对时间，例如 30_days）")
    parser.add_argument("--until", "-u", help="结束时间（默认当前时间）")
    parser.add_argument("--token", "-t", help="GitHub Token (可选，提高 API 限额)")
    parser.add_argument("--proxy", "-p", help="SOCKS5 代理 (例如 socks5://127.0.0.1:1080)")
    parser.add_argument("--stars", action="store_true", default=True,
                        help="统计 Star 数量 (默认开启)")
    parser.add_argument("--commits", action="store_true", default=True,
                        help="统计 Commit 数量 (默认开启)")
    parser.add_argument("--prs", action="store_true", default=True,
                        help="统计 PR 数量 (默认开启)")
    parser.add_argument("--forks", action="store_true", default=True,
                        help="统计 Fork 数量 (默认开启)")
    parser.add_argument("--contributors", action="store_true", default=True,
                        help="统计 Contributor 数量 (默认开启)")
    parser.add_argument("--no-stars", action="store_true", dest="no_stars",
                        help="不统计 Star")
    parser.add_argument("--no-commits", action="store_true", dest="no_commits",
                        help="不统计 Commit")
    parser.add_argument("--no-prs", action="store_true", dest="no_prs",
                        help="不统计 PR")
    parser.add_argument("--no-forks", action="store_true", dest="no_forks",
                        help="不统计 Fork")
    parser.add_argument("--no-contributors", action="store_true", dest="no_contributors",
                        help="不统计 Contributor")

    args = parser.parse_args()

    # Token 优先级: 命令行 > 环境变量
    token = args.token or os.environ.get("GITHUB_TOKEN")

    since = parse_time(args.since)
    until = parse_time(args.until) if args.until else datetime.now(timezone.utc)

    # 确保 / 分隔的 owner/repo
    if "/" not in args.repo:
        print("错误: repo 格式应为 owner/repo (例如 torvalds/linux)", file=sys.stderr)
        sys.exit(1)

    owner, repo_name = args.repo.split("/", 1)

    session = build_session(args.proxy)

    print(f"仓库:    {args.repo}")
    print(f"起始:    {since.isoformat()}")
    print(f"结束:    {until.isoformat()}")
    if token:
        print("认证:    使用 Token")
    if args.proxy:
        print(f"代理:    {args.proxy}")
    print("-" * 50)

    # 决定统计哪些项
    do_stars = args.stars and not args.no_stars
    do_commits = args.commits and not args.no_commits
    do_prs = args.prs and not args.no_prs
    do_forks = args.forks and not args.no_forks
    do_contributors = args.contributors and not args.no_contributors

    try:
        # 先获取 commit 数据（供后续 commit 数量和 contributor 复用）
        commits_data = None
        if do_commits or do_contributors:
            print("正在获取 Commit 数据 ...")
            commits_data = fetch_commits(session, owner, repo_name, since, until,
                                         token)
            print(f"  Commit 数量: {len(commits_data)}")

        if do_forks:
            print("正在统计 Fork ...")
            forks = count_forks(session, owner, repo_name, since, until, token)
            print(f"  Fork 新增: {forks}")

        if do_prs:
            print("正在统计 PR ...")
            prs = count_prs(session, owner, repo_name, since, until, token)
            print(f"  PR 新增: {prs}")

        if do_stars:
            print("正在统计 Star ...")
            stars = count_stars(session, owner, repo_name, since, until, token)
            print(f"  Star 新增: {stars}")

        if do_contributors and commits_data is not None:
            contributors = count_unique_contributors(commits_data)
            print(f"  Contributor 数: {contributors}")

        # Watch 数：API 只返回当前总数，无法获取时间范围内的新增
        repo_info = fetch_repo_info(session, owner, repo_name, token)
        print(f"\n当前 Watch: {repo_info['subscribers_count']}  (当前总数，API 不支持按时间查询)")

    except requests.exceptions.HTTPError as e:
        print(f"HTTP 错误: {e}", file=sys.stderr)
        if e.response is not None and e.response.status_code == 404:
            print("  仓库不存在或没有访问权限", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.ConnectionError as e:
        print(f"连接失败 (检查代理/网络): {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
