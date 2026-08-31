# -*- coding: utf-8 -*-
"""1688 以图搜图（禁止文本/标题搜索）。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlparse

import httpx
from loguru import logger
from playwright.async_api import Page

from .browser import BrowserSession
from .errors import ImageSearchNavError, ManualActionRequired, UploadMetadataRejected
from .upload_diagnostics import UploadDiagnostics

# 上传后必须进入的结果页 URL 特征（禁止停留在纯首页解析）
RESULT_URL_RE = re.compile(
    r"(youyuan|image[_-]?search|imgsearch|offer_search|/tu|/simil|"
    r"search/image|selloffer|img\.|picture|/pic/|imageId=|imgid=|"
    r"imageAddress=|page/offerlist|offerlist\.htm|pc-image-search|"
    r"air\.1688\.com)",
    re.I,
)
HOME_ONLY_RE = re.compile(r"^https?://((www|s)\.)?1688\.com/?(\?.*)?$", re.I)
# 注意：不要用 [class*=similar]，首页推荐「找相似」会误判
RESULT_DOM_SELECTORS = (
    "[class*='youyuan']",
    "[class*='img-list']",
    "[class*='image-list']",
    "[class*='sm-offer']",
    "[class*='offerlist']",
    "[class*='OfferList']",
    "div[data-spm*='youyuan']",
)

# 兼容多种 offer 链接形态
OFFER_ID_RE = re.compile(
    r"(?:detail(?:\.m)?\.1688\.com/offer/|offer/)(\d{8,})(?:\.html)?",
    re.I,
)
OFFER_PARAM_RE = re.compile(r"[?&](?:offerId|offer_id)=(\d{8,})", re.I)
PRICE_RE = re.compile(r"(\d+(?:\.\d+)?)")
YEN_PRICE_RE = re.compile(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)")
LABELED_PRICE_RE = re.compile(
    r"(?:价格|单价|促销价|券后价|批发价|售价)\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)"
)
MOQ_AFTER_RE = re.compile(r"\s*[件个箱包只台套米]")
# 运费/邮费标签：只看金额左侧上下文，避免「¥36 运费¥5」误杀 36
SHIPPING_LABEL_RE = re.compile(
    r"(运费|邮费|快递费|物流费|配送费|货运费|运输费|到付|续重)\s*[:：]?\s*[¥￥]?\s*$"
)
SHIPPING_CTX_RE = re.compile(
    r"(运费|邮费|快递费|物流费|配送费|货运费|运输费|到付|偏远|续重)"
)
PRODUCT_PRICE_CTX_RE = re.compile(
    r"(价格|单价|促销价|券后价|批发价|售价|成交价|拿货价)"
)

STREAM_UPLOAD_URL = (
    "https://stream-upload.taobao.com/api/upload.api"
    "?appkey=1688search&folderId=0&_input_charset=utf-8&useGtrSessionFilter=false"
)

NETWORK_PAGE_FAILURE_MARKERS = (
    "failed to fetch",
    "net::err_",
    "page crashed",
    "page has crashed",
    "target closed",
    "browser has been closed",
    "connection closed",
    "execution context was destroyed",
    "frame was detached",
)

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _plugin_version() -> str:
    try:
        data = json.loads((_PLUGIN_ROOT / "manifest.json").read_text(encoding="utf-8"))
        return str(data.get("version") or "?")
    except Exception:  # noqa: BLE001
        return "?"


PLUGIN_VERSION = _plugin_version()
NAVIGATION_TIMEOUT_MS = int(os.getenv("IMAGE_SEARCH_NAVIGATION_TIMEOUT_MS", "25000"))


def _report_progress(message: str) -> None:
    """把图搜的当前阶段显示给本地控制台，避免用户只看到文件名。"""
    try:
        from .pipeline import set_progress_message

        set_progress_message(message)
    except Exception:  # noqa: BLE001
        pass


def _audit(step: str, **details: Any) -> None:
    try:
        from .pipeline import record_technical_event

        record_technical_event(step, **details)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class SearchHit:
    url: str
    title: str = ""  # 商品名称
    shop_name: str = ""  # 店铺/公司名
    price: float | None = None
    price_text: str = ""
    extra: str = ""  # 卡片上其他简要信息


def _offer_url(offer_id: str) -> str:
    return f"https://detail.1688.com/offer/{offer_id}.html"


def _collect_offer_ids(text: str) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for rx in (OFFER_ID_RE, OFFER_PARAM_RE):
        for m in rx.finditer(text or ""):
            oid = m.group(1)
            if oid not in seen:
                seen.add(oid)
                ids.append(oid)
    return ids


def _is_shipping_blob(text: str) -> bool:
    """整段文案主要在讲运费时，整段丢弃。"""
    t = text or ""
    if not SHIPPING_CTX_RE.search(t):
        return False
    # 同时有商品价语义则仍可用（由 parse_price 局部过滤运费 ¥）
    if PRODUCT_PRICE_CTX_RE.search(t):
        return False
    # 「包邮」单独出现可保留；纯运费块丢掉
    if "包邮" in t and not re.search(r"(运费|邮费|快递费|物流费)", t):
        return False
    return True


def _collect_yen_product_prices(text: str) -> list[float]:
    """收集带 ¥ 的金额；仅排除「运费¥x / ¥x运费」这类运费金额。"""
    cleaned = str(text).replace(",", "").replace("，", "")
    vals: list[float] = []
    for m in YEN_PRICE_RE.finditer(cleaned):
        v = float(m.group(1))
        if not (0.05 <= v <= 500_000):
            continue
        start, end = m.span()
        left = cleaned[max(0, start - 14) : start]
        right = cleaned[end : min(len(cleaned), end + 8)]
        # 「运费¥8」「邮费：¥10」
        if SHIPPING_LABEL_RE.search(left):
            continue
        # 「¥8运费」：运费紧跟金额且后面不再跟新的价签
        if re.match(r"\s*(运费|邮费|快递费|物流费)(?!\s*[¥￥\d])", right):
            continue
        vals.append(v)
    return vals


def parse_price(text: str) -> float | None:
    """解析商品标价。优先标签价/¥，排除运费与起批数量。"""
    if not text:
        return None
    cleaned = str(text).replace(",", "").replace("，", "")

    # 1) 优先：带「价格/单价」标签的数字
    labeled: list[float] = []
    for m in LABELED_PRICE_RE.finditer(cleaned):
        v = float(m.group(1))
        if not (0.05 <= v <= 500_000):
            continue
        left = cleaned[max(0, m.start() - 10) : m.start()]
        if SHIPPING_LABEL_RE.search(left):
            continue
        labeled.append(v)
    if labeled:
        return min(labeled)

    # 2) ¥/￥，排除运费标签金额
    yen_vals = _collect_yen_product_prices(cleaned)
    if yen_vals:
        return min(yen_vals)

    # 3) 裸数字回退（严格）
    for m in PRICE_RE.finditer(cleaned):
        raw = m.group(1)
        v = float(raw)
        if not (0.05 <= v <= 500_000):
            continue
        end = m.end()
        start = m.start()
        after = cleaned[end : end + 2]
        left = cleaned[max(0, start - 14) : start]
        right = cleaned[end : end + 6]
        window = cleaned[max(0, start - 8) : end + 8]
        if MOQ_AFTER_RE.match(after):
            continue
        if "起批" in window or "起订" in window:
            continue
        if SHIPPING_LABEL_RE.search(left):
            continue
        # 「8运费」紧贴，且不是「120 运费¥8」这种后面还有价签
        if re.match(r"\s*(运费|邮费|快递费|物流费|配送费)(?!\s*[¥￥\d])", right):
            continue
        if "." not in raw and v <= 2:
            continue
        return v
    return None


def format_price(value: float | None, *, fallback: str = "") -> str:
    if value is None:
        return fallback or ""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _mime_for_path(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(ext, "image/jpeg")


def _address_candidates_from_url(url: str) -> list[str]:
    """从 CDN url 推导 1688 图搜可用的 imageAddress 候选。

    实测优先级（高→低）：
    1) 完整 CDN URL（最稳，少裂图）
    2) imgextra 相对路径 iN/uid/file.jpg
    3) 纯文件名（经常裂图，放最后兜底）
    """
    u = (url or "").strip()
    if not u:
        return []
    candidates: list[str] = []
    # 完整 URL 优先
    if u.startswith("http://") or u.startswith("https://"):
        candidates.append(u)
    marker = "/imgextra/"
    if marker in u:
        rel = u.split(marker, 1)[1].split("?", 1)[0].strip()
        if rel and "/" in rel:
            candidates.append(rel)
    name = u.rstrip("/").split("?")[0].split("/")[-1]
    # 纯文件名仅作兜底；过短/纯数字不要（会变成 imageAddress=10）
    if name and ("." in name or len(name) > 20) and not name.isdigit():
        candidates.append(name)
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _parse_upload_payload(payload: object) -> dict[str, str]:
    """从上传接口响应里抽出 imageId / url / fileId。

    注意：绝不能把本地 fileName（如 10.jpg → "10"）当成 imageAddress，
    否则结果页会 Failed to load image: 10。
    """
    out: dict[str, str] = {}
    if payload is None:
        return out
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return out
    if not isinstance(payload, dict):
        return out

    stack: list[object] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for k, v in cur.items():
                lk = str(k).lower()
                if isinstance(v, (dict, list)):
                    stack.append(v)
                    continue
                if v is None:
                    continue
                sv = str(v).strip()
                if not sv or sv == "0":
                    continue
                if lk in ("imageid", "image_id", "imgid") and "imageId" not in out:
                    out["imageId"] = sv
                elif lk in ("url", "imgurl", "imageurl", "ossurl") and "url" not in out:
                    out["url"] = sv
                elif lk in ("fileid", "file_id") and "fileId" not in out:
                    out["fileId"] = sv
                elif lk in ("imageaddress", "image_address") and "imageAddress" not in out:
                    # 仅接受显式 imageAddress，且不能是纯短数字/短文件名
                    if "/" in sv or "http" in sv or len(sv) > 20:
                        out["imageAddress"] = sv
        elif isinstance(cur, list):
            stack.extend(cur)

    # 优先用 CDN url 推导真正的 imageAddress
    if out.get("url"):
        addrs = _address_candidates_from_url(out["url"])
        if addrs:
            out["imageAddress"] = addrs[0]
            out["imageAddressCandidates"] = "\n".join(addrs)
    return out


def _build_youyuan_urls(meta: dict[str, str]) -> list[str]:
    """构造尽量少、成功率高的结果页 URL。

    实测：完整 CDN 作为 imageAddress 最稳；相对路径/文件名常裂图。
    因此默认只打 1～2 个 URL：air(完整CDN) → youyuan(完整CDN)；失败再退相对路径。
    """
    full_url = meta.get("url", "").strip()
    image_id = meta.get("imageId", "").strip()

    addresses: list[str] = []
    if meta.get("imageAddressCandidates"):
        addresses.extend(
            a.strip() for a in meta["imageAddressCandidates"].split("\n") if a.strip()
        )
    elif meta.get("imageAddress"):
        addresses.append(meta["imageAddress"].strip())
    if full_url:
        for a in _address_candidates_from_url(full_url):
            if a not in addresses:
                addresses.append(a)

    full_addrs = [a for a in addresses if a.startswith("http://") or a.startswith("https://")]
    rel_addrs = [
        a
        for a in addresses
        if a not in full_addrs
        and ("/" in a)
        and not a.startswith("http")
    ]

    # 主路径只用完整 CDN；相对路径仅兜底 1 个
    primary = full_addrs[:1] or rel_addrs[:1]
    fallback = []
    if full_addrs and rel_addrs:
        fallback = rel_addrs[:1]
    elif not full_addrs:
        fallback = rel_addrs[1:2]

    air = "https://air.1688.com/kapp/1688-search/pc-image-search/"
    youyuan = "https://s.1688.com/youyuan/index.htm"
    urls: list[str] = []

    for address in primary:
        q = quote(address, safe="")
        urls.append(f"{air}?tab=imageSearch&imageType=oss&imageAddress={q}")
        urls.append(f"{youyuan}?tab=imageSearch&imageType=oss&imageAddress={q}")

    for address in fallback:
        q = quote(address, safe="")
        urls.append(f"{air}?tab=imageSearch&imageType=oss&imageAddress={q}")

    if not urls and image_id:
        q = quote(image_id, safe="")
        urls.append(f"{air}?tab=imageSearch&imageId={q}&imageIdList={q}")

    seen: set[str] = set()
    uniq: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


async def _upload_image_via_browser(page: Page, image_path: Path) -> dict[str, str]:
    """在已登录页面内 fetch 上传，复用浏览器 Cookie，不点预览弹窗。"""
    raw = image_path.read_bytes()
    if len(raw) < 500:
        raise RuntimeError(f"图片过小: {image_path}")
    b64 = base64.b64encode(raw).decode("ascii")
    mime = _mime_for_path(image_path)
    name = image_path.name or "upload.jpg"

    if "1688.com" not in (page.url or ""):
        _report_progress("正在打开 1688 图搜首页")
        await page.goto(
            "https://www.1688.com/",
            wait_until="domcontentloaded",
            timeout=NAVIGATION_TIMEOUT_MS,
        )
        await page.wait_for_timeout(800)

    _report_progress(
        f"正在请求 1688 图片上传服务（最长 {NAVIGATION_TIMEOUT_MS // 1000} 秒）"
    )
    _audit(
        "image_upload_request",
        url=STREAM_UPLOAD_URL,
        file=image_path.name,
        bytes=len(raw),
        timeout_ms=NAVIGATION_TIMEOUT_MS,
    )
    started = time.perf_counter()
    diagnostics = UploadDiagnostics(page, image_path)
    diagnostics.start()
    try:
        result = await asyncio.wait_for(
            page.evaluate(
                """async ({b64, name, mime, uploadUrl}) => {
          const bin = atob(b64);
          const arr = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          const blob = new Blob([arr], {type: mime});
          const fd = new FormData();
          fd.append('file', blob, name);
          const resp = await fetch(uploadUrl, {
            method: 'POST',
            body: fd,
            credentials: 'include',
          });
          const text = await resp.text();
          return { status: resp.status, text: text.slice(0, 8000) };
                }""",
                {
                    "b64": b64,
                    "name": name,
                    "mime": mime,
                    "uploadUrl": STREAM_UPLOAD_URL,
                },
            ),
            timeout=NAVIGATION_TIMEOUT_MS / 1000,
        )
    except TimeoutError as exc:
        diagnostic_summary = diagnostics.stop()
        _audit(
            "image_upload_timeout",
            url=STREAM_UPLOAD_URL,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            **diagnostic_summary,
        )
        if diagnostic_summary["request_count"] == 0:
            raise ImageSearchNavError(
                f"上传 JS 未触发任何网络请求；诊断日志: {diagnostic_summary['log_path']}"
            ) from exc
        if diagnostic_summary["failed_count"]:
            raise ImageSearchNavError(
                f"上传请求发生连接层失败；诊断日志: {diagnostic_summary['log_path']}"
            ) from exc
        raise ImageSearchNavError(
            f"上传请求已发出但 {NAVIGATION_TIMEOUT_MS // 1000} 秒内未收到响应；"
            f"诊断日志: {diagnostic_summary['log_path']}"
        ) from exc
    except Exception:
        diagnostics.stop()
        raise
    diagnostic_summary = diagnostics.stop()
    status = int((result or {}).get("status") or 0)
    text = str((result or {}).get("text") or "")
    logger.info("stream-upload status={} body={}", status, text[:240])
    _audit(
        "image_upload_response",
        url=STREAM_UPLOAD_URL,
        http_status=status,
        elapsed_ms=round((time.perf_counter() - started) * 1000),
        response_summary=text[:500],
        **diagnostic_summary,
    )
    if status >= 400 or not text:
        raise RuntimeError(f"图片上传失败 HTTP {status}: {text[:200]}")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"上传响应非 JSON: {text[:200]}") from exc

    meta = _parse_upload_payload(payload)
    if not meta.get("imageId") and not meta.get("imageAddress") and not meta.get("url"):
        raise RuntimeError(f"上传成功但未解析到 imageId/imageAddress: {text[:300]}")
    logger.info("上传解析 meta={}", meta)
    return meta


async def _page_has_search_cards(page: Page) -> bool:
    """air 新图搜页卡片检测（无 detail.1688.com/offer 链接）。"""
    try:
        n = await page.locator(
            '[class*="searchOfferWrapper"], [class*="searchOfferItem"]'
        ).count()
        if n > 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        html = await page.content()
    except Exception:  # noqa: BLE001
        return False
    if "searchOfferWrapper" in html or "searchOfferItem" in html:
        return True
    # 旧 youyuan 页仍可能有 offer 链接
    return bool(_collect_offer_ids(html))


async def _page_looks_empty_image_search(page: Page) -> bool:
    """imageAddress 错误时常见：裂图 +「空空如也」。应换下一个候选 URL。"""
    try:
        body = await page.inner_text("body", timeout=3000)
    except Exception:  # noqa: BLE001
        return False
    # 勿用「上传图片 (0/」——加载中也会短暂出现，造成误判连跳候选
    hard = ("空空如也", "Failed to load image", "图片加载失败", "请重新上传")
    return any(m in body for m in hard)


async def _goto_youyuan_result(
    page: Page, meta: dict[str, str], *, reused_metadata: bool = False
) -> Page:
    urls = _build_youyuan_urls(meta)
    if not urls:
        raise ImageSearchNavError("无可用的图搜结果 URL")

    logger.info(
        "图搜导航 v{} 候选{}个 首选={}",
        PLUGIN_VERSION,
        len(urls),
        urls[0][:160],
    )
    last_err: Exception | None = None
    explicitly_empty = False
    for url in urls:
        try:
            logger.info("打开图搜结果页: {}", url[:200])
            _report_progress("正在打开图搜结果页")
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await page.wait_for_timeout(1500)
            await _wait_captcha_cleared(page)
            if await _is_captcha_page(page):
                last_err = ImageSearchNavError("图搜结果页触发验证码")
                continue

            # 先等卡片；确认空/裂图后再换候选（避免加载中误判）
            empty_hits = 0
            for i in range(12):
                if await _page_has_search_cards(page):
                    logger.info("图搜结果页就绪: {}", page.url[:140])
                    return page
                if await _page_looks_empty_image_search(page):
                    empty_hits += 1
                    # 连续 2 次且已过约 2.5s，才认定裂图
                    if empty_hits >= 2 and i >= 2:
                        explicitly_empty = True
                        logger.warning("结果页为空/裂图，换下一候选: {}", page.url[:140])
                        break
                else:
                    empty_hits = 0
                await page.wait_for_timeout(700)
            else:
                if _is_result_url(page.url) and not await _page_looks_empty_image_search(page):
                    logger.info("已进入图搜页（继续解析）: {}", page.url[:140])
                    return page
            last_err = ImageSearchNavError(f"结果页无 offer: {page.url[:100]}")
        except ManualActionRequired:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("打开结果页失败: {} err={}", url[:100], exc)
    if reused_metadata and explicitly_empty:
        raise UploadMetadataRejected(str(last_err or "上传元数据已失效"))
    raise ImageSearchNavError(str(last_err or "无法打开图搜结果页"))

async def _download_image(url: str, dest: Path) -> Path:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Referer": "https://www.1688.com/",
    }
    async with httpx.AsyncClient(timeout=40.0, follow_redirects=True, headers=headers) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    if dest.stat().st_size < 800:
        raise RuntimeError(f"图片过小，可能下载失败: {url[:80]}")
    return dest


async def _ensure_local_image(image_ref: str, tmp_dir: Path, idx: int) -> Path | None:
    ref = (image_ref or "").strip()
    if not ref:
        return None
    parsed = urlparse(ref)
    if parsed.scheme in ("http", "https"):
        suffix = Path(parsed.path).suffix.lower() or ".jpg"
        if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            suffix = ".jpg"
        dest = tmp_dir / f"img_{idx}{suffix}"
        try:
            await _download_image(ref, dest)
            return dest
        except Exception as exc:  # noqa: BLE001
            logger.warning("下载图片失败 {}: {}", ref[:80], exc)
            return None
    path = Path(ref)
    if path.is_file():
        return path
    logger.warning("无法识别图片路径: {}", ref[:80])
    return None


CAPTCHA_MARKERS = (
    "验证码拦截",
    "nc_token",
    "x5secdata",
    "哎哟喂，被挤爆啦",
    "滑动验证",
    "请完成验证",
    "captcha",
)


async def _page_title(page: Page) -> str:
    try:
        return (await page.title() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def _is_captcha_page(page: Page) -> bool:
    title = await _page_title(page)
    if "验证码" in title or "拦截" in title:
        return True
    current_url = (page.url or "").lower()
    if any(
        marker in current_url
        for marker in (
            "x5sec",
            "punish",
            "captcha",
            "nocaptcha",
            "verify",
            "security-check",
        )
    ):
        return True

    # 只认页面上实际存在且可见的验证组件。1688 正常工作台也会加载
    # nc_token 等风控脚本，不能仅凭源码关键字把正常页面判成验证码页。
    verification_selectors = (
        "#nocaptcha",
        "#nc_1_wrapper",
        ".nc-container",
        "#baxia-dialog-content",
        'iframe[src*="captcha"]',
        'iframe[src*="punish"]',
        '[class*="captcha-container"]',
    )
    for selector in verification_selectors:
        try:
            locator = page.locator(selector)
            if await locator.count() > 0 and await locator.first.is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue

    try:
        body_text = (await page.locator("body").inner_text(timeout=1500)).strip().lower()
    except Exception:  # noqa: BLE001
        body_text = ""
    return any(
        marker in body_text
        for marker in (
            "验证码拦截",
            "请完成验证",
            "请拖动滑块",
            "滑动验证",
            "安全验证后继续",
        )
    )


async def _has_file_input(page: Page) -> bool:
    try:
        return await page.locator('input[type="file"]').count() > 0
    except Exception:  # noqa: BLE001
        return False


async def _wait_captcha_cleared(page: Page) -> None:
    """验证出现即交还给用户；绝不尝试自动处理或反复刷新验证页。"""
    if not await _is_captcha_page(page):
        return
    title = await _page_title(page)
    logger.warning("1688 图搜触发验证（title={}），暂停等待人工处理", title)
    raise ManualActionRequired(
        "1688 页面要求人工验证。请在可见浏览器窗口完成验证后，回到工具点击“继续”。",
        kind="verification",
    )


def is_recoverable_page_network_error(exc: BaseException) -> bool:
    """只识别页面/网络运行环境损坏；业务无结果和验证码不在此列。"""
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in message for marker in NETWORK_PAGE_FAILURE_MARKERS)


async def recover_page_after_network_error(
    session: BrowserSession,
    exc: BaseException,
) -> Page:
    """刷新损坏的 1688 页面；页面已关闭时新建标签页。

    验证页必须先交还用户，绝不通过刷新反复请求或尝试绕过验证。
    """
    page = session.page
    if page is not None and not page.is_closed():
        await _wait_captcha_cleared(page)
        try:
            logger.warning("NETWORK_PAGE_RECOVERY reload url={} error={}", page.url[:160], exc)
            await page.reload(wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await page.wait_for_timeout(1200)
            await _wait_captcha_cleared(page)
            session.adopt_page(page)
            return page
        except ManualActionRequired:
            raise
        except Exception as reload_exc:  # noqa: BLE001
            logger.warning("NETWORK_PAGE_RECOVERY reload_failed error={}", reload_exc)

    context = session.context
    if context is None:
        raise ImageSearchNavError(f"1688 网络异常后无法恢复浏览器上下文: {exc}")
    page = await context.new_page()
    session.adopt_page(page)
    await page.goto(
        "https://www.1688.com/",
        wait_until="domcontentloaded",
        timeout=NAVIGATION_TIMEOUT_MS,
    )
    await page.wait_for_timeout(1200)
    await _wait_captcha_cleared(page)
    logger.warning("NETWORK_PAGE_RECOVERY new_page_ready url={}", page.url[:160])
    return page


async def _goto_image_search_home(page: Page) -> None:
    custom = os.getenv("IMAGE_SEARCH_URL", "").strip()
    # 先官网首页再 youyuan：首页登录态下点相机，有时比直接进 youyuan 少拦截
    urls = ([custom] if custom else []) + [
        "https://www.1688.com/",
        "https://s.1688.com/youyuan/index.htm",
        "https://s.1688.com/",
    ]
    last_err: Exception | None = None
    for url in urls:
        if not url:
            continue
        try:
            _report_progress("正在打开 1688 图搜入口")
            await page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await page.wait_for_timeout(1200)
            await _wait_captcha_cleared(page)
            return
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning("打开图搜页失败 {}: {}", url, exc)
    raise RuntimeError(f"无法打开 1688 图搜页: {last_err}")


async def _open_image_search_panel(page: Page) -> None:
    """点开首页「以图搜款」面板（1688 首页 imageUpload 区域）。"""
    candidates = [
        page.locator("[data-tracker='imgSearch']").first,
        page.locator(".image-upload-button-container").first,
        page.locator(".image-upload-button-camera").first,
        page.get_by_text("以图搜款", exact=False).first,
        page.get_by_text("以图搜图", exact=False).first,
        page.locator("i[class*='camera']").first,
        page.locator("[class*='image-search']").first,
    ]
    for loc in candidates:
        try:
            if await loc.count() > 0:
                await loc.click(timeout=2500)
                await page.wait_for_timeout(800)
                await _wait_captcha_cleared(page)
                return
        except RuntimeError:
            raise
        except Exception:  # noqa: BLE001
            continue


async def _dispatch_file_events(page: Page) -> None:
    try:
        await page.evaluate(
            """() => {
              const el = document.getElementById('img-search-upload')
                || document.querySelector('input.image-file-reader-wrapper');
              if (!el) return false;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
              return true;
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("dispatch file events failed: {}", exc)


async def _search_confirm_visible(page: Page) -> bool:
    try:
        loc = page.locator("div.search-btn").filter(has_text="搜索图片")
        if await loc.count() == 0:
            loc = page.get_by_text("搜索图片", exact=True)
        if await loc.count() == 0:
            return False
        return await loc.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


async def _hard_click_search_confirm(page: Page) -> str:
    """通过正常控件触发「搜索图片」。不合成鼠标轨迹或事件序列。"""
    selectors = [
        'div.search-btn[data-tracker="pasteImagePreview"]',
        "div.search-btn",
        '[data-tracker="pasteImagePreview"]',
    ]
    for sel in selectors:
        loc = page.locator(sel).filter(has_text="搜索图片")
        if await loc.count() == 0:
            loc = page.locator(sel)
        if await loc.count() == 0:
            continue
        btn = loc.first
        try:
            await btn.scroll_into_view_if_needed(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
        try:
            await btn.click(timeout=3000, no_wait_after=True)
            logger.info("正常点击「搜索图片」 via {}", sel)
            return f"locator:{sel}"
        except Exception as exc:  # noqa: BLE001
            logger.debug("正常点击失败 {}: {}", sel, exc)
    return ""


async def _wait_after_search_click(
    page: Page,
    *,
    session: BrowserSession,
    before_url: str,
    timeout_ms: int = 35_000,
) -> Page:
    """点击后等待：弹窗关闭 / URL 变化 / 新标签 / 结果特征。返回工作页。"""
    ctx = session.context
    deadline = timeout_ms
    step = 500
    elapsed = 0
    work = page

    while elapsed < deadline:
        # 新标签？
        if ctx is not None:
            for p in ctx.pages:
                if p.is_closed():
                    continue
                try:
                    u = p.url or ""
                except Exception:  # noqa: BLE001
                    continue
                if u and u != before_url and not HOME_ONLY_RE.match(u):
                    logger.info("检测到新页面/跳转页: {}", u[:140])
                    session.adopt_page(p)
                    try:
                        await p.bring_to_front()
                    except Exception:  # noqa: BLE001
                        pass
                    return p
                if u and _is_result_url(u):
                    session.adopt_page(p)
                    return p

        try:
            current = work.url
        except Exception:  # noqa: BLE001
            current = ""

        if _is_result_url(current):
            logger.info("当前页已是结果 URL: {}", current[:140])
            return work

        popup = await _search_confirm_visible(work)
        if not popup:
            # 弹窗没了：再等一会看 URL / DOM
            if current and current.rstrip("/") != before_url.rstrip("/"):
                logger.info("弹窗已关且 URL 变化: {} -> {}", before_url[:60], current[:100])
                return work
            if await _has_result_dom(work):
                logger.info("弹窗已关且出现结果 DOM: {}", current[:100])
                return work
            # 弹窗关了但还在首页：可能正在跳，继续等
            if elapsed > 8_000 and HOME_ONLY_RE.match(current or ""):
                # 再给一次硬点机会（有时第一次没点上）
                pass

        await work.wait_for_timeout(step)
        elapsed += step
        if elapsed in (5_000, 10_000, 20_000):
            logger.warning(
                "等待图搜跳转中… {}ms url={} popup={}",
                elapsed,
                (work.url or "")[:100],
                await _search_confirm_visible(work),
            )
            # 中途再点一次
            if await _search_confirm_visible(work):
                await _hard_click_search_confirm(work)

    return work


async def _click_search_image_confirm(
    page: Page,
    *,
    session: BrowserSession,
    before_url: str,
) -> Page:
    """上传后首页会弹出「帮你找同款」，必须点「搜索图片」才会跳结果页。"""
    # 等弹窗与缩略图就绪
    for _ in range(30):
        if await _search_confirm_visible(page):
            break
        if await page.get_by_text("帮你找同款", exact=False).count() > 0:
            break
        if await page.get_by_text("已上传", exact=False).count() > 0:
            break
        await page.wait_for_timeout(200)
    else:
        logger.warning("未检测到「搜索图片」确认弹窗")

    # 缩略图出来后再稍等，避免点早了无效
    await page.wait_for_timeout(1200)

    how = await _hard_click_search_confirm(page)
    if not how:
        logger.warning("未能点击「搜索图片」")
    else:
        logger.info("已触发搜索确认: {}", how)

    work = await _wait_after_search_click(
        page, session=session, before_url=before_url, timeout_ms=20_000
    )

    # 自动点击常被页面吞掉：仅当显式开启环境变量才等人手点
    wait_sec = int(os.getenv("MANUAL_SEARCH_CLICK_WAIT_SEC", "0"))
    if wait_sec > 0 and await _search_confirm_visible(work):
        logger.warning(
            "自动点击「搜索图片」未生效。请手动点击（仅调试用，最多 {} 秒）",
            wait_sec,
        )
        try:
            from .pipeline import set_progress_message

            set_progress_message(f"请手动点击「搜索图片」（{wait_sec}s内）")
        except Exception:  # noqa: BLE001
            pass
        for _elapsed in range(0, wait_sec, 2):
            if not await _search_confirm_visible(work):
                logger.info("检测到预览弹窗已关闭")
                break
            ctx = session.context
            if ctx is not None:
                for p in ctx.pages:
                    if p.is_closed():
                        continue
                    u = p.url or ""
                    if u and (_is_result_url(u) or (u != before_url and not HOME_ONLY_RE.match(u))):
                        session.adopt_page(p)
                        return p
            await work.wait_for_timeout(2000)
        work = await _wait_after_search_click(
            work, session=session, before_url=before_url, timeout_ms=15_000
        )
    return work


def _is_result_url(url: str) -> bool:
    u = (url or "").strip()
    if not u:
        return False
    if HOME_ONLY_RE.match(u):
        return False
    return bool(RESULT_URL_RE.search(u))


async def _has_result_dom(page: Page) -> bool:
    for sel in RESULT_DOM_SELECTORS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            continue
    # 结果页常见：图片搜索相关标题/筛选项
    try:
        body = await page.inner_text("body", timeout=2000)
        if any(k in body for k in ("图搜", "找同款", "以图搜", "相似商品", "同款比价")):
            if "帮你找同款" not in body or "搜索图片" not in body:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


async def _assert_left_homepage(page: Page, *, before_url: str) -> None:
    """熔断：未进入图搜结果页则禁止解析当前 HTML。"""
    await page.wait_for_timeout(500)
    if await _is_captcha_page(page):
        raise ImageSearchNavError("上传后仍停留在验证码页，图搜未真正开始")

    current = page.url
    if _is_result_url(current):
        logger.info("已进入图搜结果页: {}", current[:140])
        return

    # 确认弹窗还在 = 搜索根本没启动
    if await _search_confirm_visible(page):
        await _dump_debug(page, "no_nav_after_upload")
        raise ImageSearchNavError(
            "已点「搜索图片」但预览弹窗仍在，搜索未真正发起。"
            f" before={before_url[:80]} after={current[:80]}。"
            "调试页: storage/debug/no_nav_after_upload.html"
        )

    try:
        await page.wait_for_url(RESULT_URL_RE, timeout=8_000)
        current = page.url
        if _is_result_url(current):
            logger.info("延迟跳转成功: {}", current[:140])
            return
    except Exception:  # noqa: BLE001
        pass

    for _ in range(6):
        if await _has_result_dom(page):
            logger.info("检测到图搜结果特征 DOM/文案: {}", page.url[:120])
            return
        if page.url and page.url.rstrip("/") != before_url.rstrip("/") and not HOME_ONLY_RE.match(
            page.url
        ):
            logger.info("URL 已离开首页: {}", page.url[:120])
            return
        await page.wait_for_timeout(1000)

    if HOME_ONLY_RE.match(page.url) or page.url.rstrip("/") == before_url.rstrip("/"):
        await _dump_debug(page, "no_nav_after_upload")
        raise ImageSearchNavError(
            "图搜未跳转到结果页（禁止解析首页假数据）。"
            f" before={before_url[:80]} after={page.url[:80]}。"
            "调试页: storage/debug/no_nav_after_upload.html"
        )

    await _dump_debug(page, "no_nav_after_upload")
    raise ImageSearchNavError(
        "图搜未确认进入结果页，已熔断。"
        f" url={page.url[:100]}"
    )


async def _try_upload(page: Page, image_path: Path, session: BrowserSession) -> Page | None:
    """优先 #img-search-upload；必须进入结果页才算成功。返回工作页。"""
    before_url = page.url
    await _wait_captcha_cleared(page)
    await _open_image_search_panel(page)
    await _wait_captcha_cleared(page)

    try:
        await page.wait_for_selector(
            "#img-search-upload, input.image-file-reader-wrapper",
            timeout=8_000,
        )
    except Exception:  # noqa: BLE001
        logger.warning("未等到 #img-search-upload，继续尝试通用 file input")

    targets: list[tuple[str, int]] = []
    for sel in (
        "#img-search-upload",
        "input.image-file-reader-wrapper",
        'input[type="file"][accept*="jpg"]',
        'input[type="file"][accept*="image"]',
        'input[type="file"]',
    ):
        loc = page.locator(sel)
        try:
            count = await loc.count()
        except Exception:  # noqa: BLE001
            continue
        for i in range(min(count, 2)):
            targets.append((sel, i))

    last_nav_err: Exception | None = None
    for sel, i in targets:
        try:
            handle = page.locator(sel).nth(i)
            logger.info("尝试上传 {} -> {} #{}", image_path.name, sel, i)
            await handle.set_input_files(str(image_path))
            await _dispatch_file_events(page)
            await page.wait_for_timeout(1000)
            work = await _click_search_image_confirm(
                page, session=session, before_url=before_url
            )
            await _wait_captcha_cleared(work)
            await _assert_left_homepage(work, before_url=before_url)
            return work
        except ImageSearchNavError as exc:
            last_nav_err = exc
            logger.warning("上传后未进入结果页 ({} #{}): {}", sel, i, exc)
            if sel in ("#img-search-upload", "input.image-file-reader-wrapper"):
                break
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("上传异常 {} #{}: {}", sel, i, exc)

    if last_nav_err:
        raise last_nav_err
    return None


async def _dump_debug(page: Page, tag: str) -> None:
    try:
        debug_dir = _PLUGIN_ROOT / "storage" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        html_path = debug_dir / f"{tag}.html"
        html_path.write_text(await page.content(), encoding="utf-8")
        logger.warning("已保存图搜调试页 {} url={}", html_path, page.url[:120])
    except Exception as exc:  # noqa: BLE001
        logger.debug("保存调试页失败: {}", exc)


async def _collect_hits_from_page(page: Page, limit: int = 40) -> list[SearchHit]:
    """解析 air.1688 图搜结果页卡片（searchOfferItem），不进详情页。"""
    await page.wait_for_timeout(2000)
    try:
        await page.wait_for_selector(
            '[class*="searchOfferWrapper"], [class*="searchOfferItem"], [class*="offerTitleRow"]',
            timeout=18_000,
        )
    except Exception:  # noqa: BLE001
        logger.warning("图搜结果区等待超时，仍尝试解析 url={}", page.url[:120])

    cards: list[dict[str, Any]] = []
    try:
        cards = await page.evaluate(
            """() => {
              const out = [];
              const seen = new Set();
              const pick = (el) => ((el && (el.innerText || el.textContent)) || '')
                .replace(/\\s+/g, ' ').trim();

              // 优先取卡片根节点，避免 searchOfferItem 嵌套重复
              let nodes = Array.from(document.querySelectorAll('[class*="searchOfferWrapper"]'));
              if (!nodes.length) {
                nodes = Array.from(document.querySelectorAll('[class*="searchOfferItem"]'));
              }

              for (const card of nodes) {
                let oid = '';
                const report = card.getAttribute('data-aplus-report') || '';
                // serverTrackId@xxx_<offerId>_<sid>^...
                let m = report.match(/serverTrackId@[^^]*?_(\\d{8,})_/);
                if (!m) m = report.match(/_(\\d{11,13})_/);
                if (!m) m = report.match(/offerId[=@](\\d{8,})/i);
                if (m) oid = m[1];
                if (!oid) {
                  const rk = card.getAttribute('data-renderkey') || '';
                  m = rk.match(/_(\\d{8,})$/);
                  if (m) oid = m[1];
                }
                if (!oid) {
                  const ww = card.querySelector('[data-extra*="offerId"], a[href*="offerId="]');
                  if (ww) {
                    const extra = (ww.getAttribute('data-extra') || '')
                      .replace(/&quot;/g, '"');
                    m = extra.match(/offerId\\D+(\\d{8,})/);
                    if (!m) {
                      const href = ww.getAttribute('href') || '';
                      m = href.match(/offerId=(\\d{8,})/);
                    }
                    if (m) oid = m[1];
                  }
                }
                if (!oid || seen.has(oid)) continue;

                let title = '';
                const titleEl = card.querySelector(
                  '[class*="titleText"], [class*="offerTitleRow"]'
                );
                if (titleEl) title = pick(titleEl);
                if (!title) {
                  const img = card.querySelector('img.mainImg, img[src*="cib"], img');
                  if (img) title = (img.getAttribute('alt') || '').trim();
                }
                title = title.replace(/[¥￥]\\s*\\d+(?:\\.\\d{1,2})?/g, '').trim();

                let shop = '';
                const shopEl = card.querySelector('[class*="shopName"]');
                if (shopEl) shop = pick(shopEl);

                // 价格：优先 aplus 的 price@（含小数）；textMain 常只显示整数位（15.30→15）
                let priceText = '';
                let priceVal = null;
                const pm = report.match(/[\\^&]price@([0-9]+(?:\\.[0-9]+)?)/);
                if (pm) {
                  priceVal = parseFloat(pm[1]);
                  priceText = '¥' + pm[1];
                }
                if (priceVal == null) {
                  const priceItem = card.querySelector(
                    '[class*="priceItem"], [class*="offerPriceRow"]'
                  );
                  if (priceItem) {
                    const raw = pick(priceItem).replace(/运费[\\s\\S]*/g, '');
                    const yen = raw.match(/[¥￥]\\s*([0-9]+(?:\\.[0-9]{1,2})?)/);
                    const num = yen
                      ? yen[1]
                      : (raw.match(/([0-9]+(?:\\.[0-9]{1,2})?)/) || [])[1];
                    if (num) {
                      priceVal = parseFloat(num);
                      priceText = '¥' + num;
                    }
                  }
                }
                if (priceVal == null) {
                  const text = pick(card);
                  const yen = text.match(/[¥￥]\\s*([0-9]+(?:\\.[0-9]{1,2})?)/);
                  if (yen) {
                    priceVal = parseFloat(yen[1]);
                    priceText = '¥' + yen[1];
                  }
                }
                if (priceVal == null || !(priceVal >= 0.05 && priceVal <= 500000)) continue;

                const descs = Array.from(card.querySelectorAll('[class*="descText"]'))
                  .map(pick)
                  .filter((t) => t && !/^运费/.test(t) && !/元起$/.test(t))
                  .slice(0, 4);
                const extra = descs.join('；');

                seen.add(oid);
                out.push({
                  offerId: oid,
                  title: title.slice(0, 160),
                  shopName: shop.slice(0, 80),
                  priceText,
                  priceVal,
                  extra: extra.slice(0, 160),
                  cardText: pick(card).slice(0, 240),
                });
                if (out.length >= 80) break;
              }
              return out;
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("结果页卡片 JS 解析失败: {}", exc)
        cards = []

    hits: list[SearchHit] = []
    for c in (cards or [])[:limit]:
        oid = str(c.get("offerId") or "")
        if not oid:
            continue
        price_text = str(c.get("priceText") or "").replace("\n", "").replace("\r", "").strip()
        price = None
        if c.get("priceVal") is not None:
            try:
                price = float(c["priceVal"])
            except (TypeError, ValueError):
                price = None
        if price is None:
            price = parse_price(price_text) or parse_price(str(c.get("cardText") or ""))
        if price is None:
            continue
        title = re.sub(r"\s+", " ", str(c.get("title") or "")).strip()
        shop = re.sub(r"\s+", " ", str(c.get("shopName") or "")).strip()
        extra = re.sub(r"\s+", " ", str(c.get("extra") or "")).strip()
        if shop and shop == title:
            shop = ""
        hits.append(
            SearchHit(
                url=_offer_url(oid),
                title=title[:200],
                shop_name=shop[:80],
                price=price,
                price_text=price_text or format_price(price),
                extra=extra[:120],
            )
        )

    logger.info(
        "结果页卡片解析到 {} 条（有标题 {} / 有店铺名 {}；保持页面相似度顺序）",
        len(hits),
        sum(1 for h in hits if len(h.title) >= 4),
        sum(1 for h in hits if h.shop_name),
    )
    for h in hits[:3]:
        logger.info(
            "样例(按相似度) offer={} price={} shop={!r} title={!r}",
            h.url.rstrip("/").split("/")[-1].replace(".html", ""),
            h.price_text,
            (h.shop_name or "")[:30],
            (h.title or "")[:40],
        )

    # 禁止按价格重排：页面顺序≈图搜相似度；价格排序会把配件/贴纸顶到最前
    return hits


async def search_by_images(
    session: BrowserSession,
    image_urls: list[str],
    *,
    max_images: int = 5,
    per_image_limit: int = 30,
    upload_metadata: dict[str, Any] | None = None,
    on_upload: Callable[[dict[str, str]], Any] | None = None,
    on_result_opened: Callable[[str], Any] | None = None,
) -> list[SearchHit]:
    """多图搜图，合并去重。绝不改用商品名文本搜索。

    主路径：浏览器 Cookie 上传拿 imageAddress/imageId → 直接打开 youyuan 结果页。
    不依赖首页「搜索图片」按钮（该按钮对自动化点击经常无响应）。
    """
    if not image_urls:
        raise RuntimeError("没有可用于图搜的图片")

    page = await session.start()
    merged: dict[str, SearchHit] = {}
    uploaded_ok = 0
    nav_errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="1688img_") as tmp:
        tmp_dir = Path(tmp)
        order = list(range(min(len(image_urls), max_images)))
        for idx in order:
            ref = image_urls[idx]
            local = await _ensure_local_image(ref, tmp_dir, idx)
            if local is None:
                continue
            try:
                # 确保登录态 Cookie 可用
                if "1688.com" not in (page.url or ""):
                    _report_progress("正在确认 1688 登录状态")
                    await page.goto(
                        "https://www.1688.com/",
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    await page.wait_for_timeout(800)
                await _wait_captcha_cleared(page)

                reused_metadata = bool(upload_metadata)
                if reused_metadata:
                    meta = {str(k): str(v) for k, v in upload_metadata.items() if v is not None}
                    _report_progress("复用已保存的上传结果，正在读取图搜结果")
                    _audit("upload_metadata_reused", file=local.name)
                else:
                    _report_progress(f"正在上传图片 {local.name}")
                    meta = await _upload_image_via_browser(page, local)
                    if on_upload is not None:
                        callback_result = on_upload(dict(meta))
                        if inspect.isawaitable(callback_result):
                            await callback_result
                _report_progress("上传完成，正在读取图搜结果")
                page = await _goto_youyuan_result(
                    page, meta, reused_metadata=reused_metadata
                )
                session.adopt_page(page)
                if on_result_opened is not None:
                    callback_result = on_result_opened(page.url or "")
                    if inspect.isawaitable(callback_result):
                        await callback_result

                if HOME_ONLY_RE.match(page.url or ""):
                    raise ImageSearchNavError(
                        f"第 {idx + 1} 张上传后仍停在首页: {page.url[:100]}"
                    )

                uploaded_ok += 1
                _report_progress("正在解析图搜结果")
                hits = await _collect_hits_from_page(page, limit=per_image_limit)
                logger.info(
                    "第 {} 张图搜命中 {} 条 url={}",
                    idx + 1,
                    len(hits),
                    page.url[:100],
                )
                if not hits:
                    await _dump_debug(page, f"no_hits_{idx}")
                for hit in hits:
                    old = merged.get(hit.url)
                    if old is None:
                        merged[hit.url] = hit
                    elif hit.price is not None and (
                        old.price is None or hit.price < old.price
                    ):
                        merged[hit.url] = hit
                    elif not old.title and hit.title:
                        old.title = hit.title
                if len(merged) >= 8:
                    break
            except ManualActionRequired:
                # 必须原样交给上层暂停当前任务；禁止包装成普通导航失败后跳到下一张。
                raise
            except ImageSearchNavError as exc:
                nav_errors.append(str(exc))
                logger.error("第 {} 张图搜导航熔断: {}", idx + 1, exc)
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception("图搜失败 img#{}: {}", idx + 1, exc)
                nav_errors.append(str(exc))
            await session.delay()

    if uploaded_ok == 0:
        if nav_errors:
            raise ImageSearchNavError(nav_errors[0])
        raise ImageSearchNavError(
            "未能完成真正的 1688 图搜（图片上传或结果页打开失败）。"
            "请确认已登录 1688。"
        )

    results = list(merged.values())
    if not results:
        raise ImageSearchNavError(
            f"已进入结果页但未解析到 offer（上传成功 {uploaded_ok} 次）。"
            "请检查 storage/debug/no_hits_*.html"
        )

    # 保持首次出现顺序（≈单图结果页相似度），不要在这里按价格重排
    logger.info("图搜合并去重后 {} 条（真正成功上传并跳转 {} 张）", len(results), uploaded_ok)
    return results
