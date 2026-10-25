#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generic Blackboard bulk archiver.
Supports both Original View (HTML Scrape) and Ultra Course View (REST API).
Optimized for streaming downloads.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import argparse
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import (
    sync_playwright,
    BrowserContext,
    APIRequestContext,
)

parser = argparse.ArgumentParser(description="Generic Blackboard Archiver")
parser.add_argument("--url", required=True, help="Base URL of the Blackboard instance (e.g., https://lms2017.teikyo-u.ac.jp)")
parser.add_argument("--out", default="out", help="Output directory")
parser.add_argument("--courses", default="", help="Comma-separated list of course IDs to download")
parser.add_argument("--dry-run", action="store_true", help="Enumerate only, no downloads")
parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
parser.add_argument("--verbose", action="store_true", help="Verbose logging")
args = parser.parse_args()

BASE = args.url.rstrip("/")
ROOT = Path(__file__).resolve().parent
OUT = Path(args.out).resolve()
PROFILE_DIR = ROOT / ".pw_profile"
DRY = args.dry_run
HEADLESS = args.headless
ONLY = {c.strip() for c in args.courses.split(",") if c.strip()}
VERBOSE = args.verbose

SAFE_NAME = re.compile(r'[\\/*?:"<>|\r\n\t]+')

DL_SESSION = requests.Session()
DL_SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

def safe(name: str, maxlen: int = 150) -> str:
    name = SAFE_NAME.sub("_", name)
    name = name[:maxlen]
    name = name.strip().strip(".")
    return name or "untitled"

def log(msg: str) -> None:
    print(msg, flush=True)

def vlog(msg: str) -> None:
    if VERBOSE:
        print(f"  ・ {msg}", flush=True)
        
@dataclass
class Course:
    course_id: str
    pk1: str
    name: str
    role: str = "Student"
    available: str = "Yes"
    out_dir: Path = field(init=False)

    def __post_init__(self):
        # バグ修正: コースフォルダ名から英字のシステムIDを除外し、科目名のみにする
        self.out_dir = OUT / safe(self.name)

def ensure_login(context: BrowserContext) -> None:
    page = context.new_page()
    log(f"Opening Blackboard ({BASE}) to verify session...")
    api = context.request
    
    try:
        r = api.get(f"{BASE}/learn/api/public/v1/users/me")
        if r.ok and '"userName"' in r.text():
            log("  ✓ Already authenticated.")
            page.close()
            return
    except Exception:
        pass

    log("=" * 70)
    log("LOGIN REQUIRED")
    log("A Chromium window is open. Sign in to your Blackboard.")
    log("Script will auto-continue once it detects a valid session. Up to 10 min.")
    log("=" * 70)
    page.goto(f"{BASE}/", wait_until="domcontentloaded")
    deadline = time.time() + 600
    last_log = 0.0
    while time.time() < deadline:
        time.sleep(3)
        try:
            r = api.get(f"{BASE}/learn/api/public/v1/users/me")
            if r.ok and '"userName"' in r.text():
                log("  ✓ Authenticated (API).")
                page.close()
                return
            
            current_url = page.url
            if "/webapps/portal/" in current_url or "/ultra" in current_url or "/webapps/blackboard/" in current_url:
                log("  ✓ Authenticated (URL check).")
                page.close()
                return
        except Exception:
            pass
        
        now = time.time()
        if now - last_log > 20:
            log("  ...waiting for login...")
            last_log = now
            
    raise SystemExit("Login timed out after 10 minutes. Aborting.")

def list_courses(api: APIRequestContext) -> list[Course]:
    log("Enumerating courses...")
    out: list[Course] = []
    next_url = f"{BASE}/learn/api/public/v1/users/me/courses?limit=200&expand=course"
    
    while next_url:
        r = api.get(next_url, timeout=30000)
        if not r.ok:
            raise SystemExit(f"course list failed: {r.status} {r.text()[:300]}")
            
        data = r.json()
        
        for m in data.get("results", []):
            c = m.get("course", {}) or {}
            avail = (c.get("availability", {}) or {}).get("available", "Unknown")
            pk1 = c.get("id") or m.get("courseId") or ""
            ext = c.get("courseId") or pk1
            name = c.get("displayName") or c.get("name") or ext
            role = m.get("courseRoleId") or "Student"
            
            if not pk1:
                continue
            if ONLY and pk1 not in ONLY and ext not in ONLY:
                continue
                
            out.append(Course(course_id=ext, pk1=pk1, name=name, role=role, available=avail))
            
        paging = data.get("paging", {})
        next_path = paging.get("nextPage")
        next_url = urljoin(BASE, next_path) if next_path else None

    out.sort(key=lambda c: c.name.lower())
    log(f"  ✓ {len(out)} courses")
    return out

def fetch_text(api: APIRequestContext, url: str) -> tuple[int, str, dict]:
    try:
        r = api.get(url, headers={"Accept": "text/html,*/*"}, timeout=30000)
        return r.status, r.text(), dict(r.headers)
    except Exception as e:
        vlog(f"fetch_text error {url}: {e}")
        return 0, "", {}

def save(path: Path, content: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8", errors="replace")
    else:
        path.write_bytes(content)

def get_course_menu(api: APIRequestContext, course: Course) -> list[dict]:
    url = f"{BASE}/webapps/blackboard/execute/courseMain?course_id={quote(course.pk1)}"
    status, html, _ = fetch_text(api, url)
    if status != 200 or not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for ul_id in ("courseMenuPalette_contents", "courseMenuPalette_paletteContents"):
        ul = soup.find("ul", id=ul_id)
        if not ul:
            continue
        for a in ul.find_all("a", href=True):
            title = a.get_text(" ", strip=True)
            href = a["href"]
            if title:
                items.append({"title": title, "href": urljoin(BASE, href)})
        if items:
            break
    seen = set()
    out = []
    for it in items:
        key = (it["title"], it["href"])
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out

def is_attachment_link(href: str) -> bool:
    # バグ修正: PDFなどをより確実に検知し、フォルダリンクと明確に区別する
    h = href.lower()
    if any(p in h for p in ("/bbcswebdav/", "/courses/", "content_id=", "/content/file", "/bbcsweb/")):
        if "listcontent.jsp" in h or "coursemain" in h or "displaylearningunit" in h:
            return False
        return True
    if h.endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".mp4", ".mp3", ".mov", ".m4a")):
        return True
    return False

def get_filename_from_response(headers, fallback_url: str) -> str:
    cd = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.IGNORECASE)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="([^"]+)"', cd, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"filename=([^;]+)", cd, re.IGNORECASE)
    if m:
        return m.group(1).strip().strip('"')
    name = os.path.basename(urlparse(fallback_url).path) or "file.bin"
    return unquote(name)

def download_file(api: APIRequestContext, url: str, dest_dir: Path, hint: str = "") -> Path | None:
    try:
        log(f"      Checking: {hint or 'file'} ...")
        with DL_SESSION.get(url, stream=True, timeout=30, allow_redirects=True) as r:
            if not r.ok:
                log(f"      [!] Download failed: HTTP {r.status_code}")
                return None
            
            # バグ修正: サーバーから取得したファイル名と、画面上の見出し(hint)を合成し綺麗な名前にする
            name_from_server = get_filename_from_response(r.headers, url)
            ext = Path(name_from_server).suffix
            if not ext:
                ctype = (r.headers.get("content-type") or "").lower()
                if "pdf" in ctype: ext = ".pdf"
                elif "mp4" in ctype: ext = ".mp4"
                elif "word" in ctype: ext = ".docx"
                elif "powerpoint" in ctype or "presentation" in ctype: ext = ".pptx"
                elif "excel" in ctype or "spreadsheet" in ctype: ext = ".xlsx"
                elif "jpeg" in ctype or "jpg" in ctype: ext = ".jpg"
                elif "png" in ctype: ext = ".png"

            final_name = name_from_server
            if hint and hint.lower() not in ("item", "file", "folder", ""):
                if Path(hint).suffix:
                    final_name = hint
                else:
                    final_name = f"{hint}{ext}"
                    
            final_name = safe(final_name)
            dest = dest_dir / final_name
            
            if dest.exists() and dest.stat().st_size > 0:
                log(f"      Skip (already exists): {dest.name}")
                return dest
                
            log(f"      Downloading: {dest.name} ...")
            ctype = (r.headers.get("content-type") or "").lower()
            if "text/html" in ctype and not final_name.lower().endswith((".html", ".htm")):
                dest = dest.with_suffix(dest.suffix + ".interstitial.html")
            
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        
            return dest
    except Exception as e:
        log(f"      [!] Download timeout/error: {e}")
        return None

def walk_content(
    api: APIRequestContext, course: Course, content_url: str, dest_dir: Path,
    crumbs: list[str], seen: set[str], depth: int = 0
) -> tuple[int, int]:
    if depth > 8: return 0, 0
    if content_url in seen: return 0, 0
    seen.add(content_url)
    
    status, html, _ = fetch_text(api, content_url)
    if status != 200: return 0, 0
    dest_dir.mkdir(parents=True, exist_ok=True)
    save(dest_dir / "_index.html", html)
    
    soup = BeautifulSoup(html, "html.parser")
    files = folders = 0
    items = soup.select("li.liItem, li.read")
    if not items: items = soup.select("ul#content_listContainer > li")

    for li in items:
        head = li.find(["h3", "h4"])
        title = (head.get_text(" ", strip=True) if head else "").strip() or "item"
        
        is_folder = False
        
        # バグ修正: サブフォルダを作る際、必ず画面の見出し（title）を利用して綺麗なフォルダ名にする
        for a in li.find_all("a", href=True):
            href = a["href"]
            if ("listContent.jsp" in href or "displayLearningUnit" in href) and not is_attachment_link(href):
                full = urljoin(BASE, href)
                sub_dir = dest_dir / safe(title)
                f1, f2 = walk_content(api, course, full, sub_dir, crumbs + [title], seen, depth + 1)
                folders += 1 + f1
                files += f2
                is_folder = True

        urls_to_download = set()
        
        # 通常の添付ファイル
        for a in li.find_all("a", href=True):
            href = a["href"]
            if is_attachment_link(href) and "listContent.jsp" not in href:
                hint = a.get_text(" ", strip=True) or title
                urls_to_download.add((href, hint))
        
        # バグ修正: 埋め込まれたPDF等の取得（iframe, embed, object）を追加
        for tag in li.find_all(["iframe", "embed", "object", "source", "img"]):
            src = tag.get("src") or tag.get("data")
            if src:
                if tag.name == "img" and not any(p in src.lower() for p in ("/bbcswebdav/", "/courses/", "/content/")):
                    continue
                if is_attachment_link(src):
                    urls_to_download.add((src, title))

        attached_any = False
        for url, hint in urls_to_download:
            full = urljoin(BASE, url)
            if not DRY:
                dl_path = download_file(api, full, dest_dir, hint=hint)
                if dl_path:
                    files += 1
            attached_any = True

        body_div = li.find("div", class_="details") or li.find("div", class_="vtbegenerated")
        item_text_parts = [title]
        if body_div: item_text_parts.append(body_div.get_text("\n", strip=True))
        else: item_text_parts.append(li.get_text("\n", strip=True))
        item_text = "\n\n".join(p for p in item_text_parts if p)
        
        item_slug = safe(title) or "item"
        desc_path = dest_dir / f"{item_slug}.txt"
        if len(item_text.strip()) > len(title):
            save(desc_path, item_text)
            
        if attached_any or is_folder:
            vlog(f"{'  ' * depth}+ {title}")

    return folders, files

def archive_course(api: APIRequestContext, course: Course) -> dict:
    course.out_dir.mkdir(parents=True, exist_ok=True)
    log(f"\n=== {course.name} ({course.pk1}) ===")
    summary = {"course_id": course.pk1, "name": course.name, "available": course.available}
    
    menu = get_course_menu(api, course)
    
    total_folders = total_files = 0
    seen: set[str] = set()
    
    if menu:
        save(course.out_dir / "_menu.json", json.dumps(menu, indent=2))
        log(f"  menu: {len(menu)} entries (Original View)")
        summary["menu_entries"] = len(menu)
        
        for entry in menu:
            href = entry["href"]
            if "listContent.jsp" not in href: continue
            # バグ修正: 2組から1組のコンテンツへ飛ぶ場合でもブロックせず素直に辿るように制限を解除
            
            section_title = safe(entry["title"]) or "content"
            section_dir = course.out_dir / section_title
            log(f"  → {entry['title']}")
            try:
                f1, f2 = walk_content(api, course, href, section_dir, [section_title], seen)
                total_folders += f1
                total_files += f2
            except Exception as e:
                log(f"  !! error walking content area {entry['title']}: {e}")
                
    else:
        log("  menu: 0 entries (Detected as Ultra Course. Switching to REST API...)")
        summary["menu_entries"] = 0
        
        def walk_ultra(folder_id: str | None, current_dir: Path, depth: int):
            nonlocal total_folders, total_files
            if depth > 10: return
            
            if folder_id is None:
                path = f"{BASE}/learn/api/public/v1/courses/{course.pk1}/contents?limit=200"
            else:
                path = f"{BASE}/learn/api/public/v1/courses/{course.pk1}/contents/{folder_id}/children?limit=200"
                
            try:
                r = api.get(path, timeout=30000)
                if not r.ok: return
                items = r.json().get("results", [])
            except Exception as e:
                vlog(f"Ultra API error: {e}")
                return
                
            for item in items:
                title = safe(item.get("title") or "item")
                content_id = item.get("id")
                handler = item.get("contentHandler", {}).get("id", "").lower()
                
                if any(x in handler for x in ["folder", "lesson", "module", "blankpage"]):
                    sub_dir = current_dir / title
                    sub_dir.mkdir(parents=True, exist_ok=True)
                    total_folders += 1
                    log(f"{'  ' * depth}+ Folder: {title}")
                    walk_ultra(content_id, sub_dir, depth + 1)
                else:
                    try:
                        att_url = f"{BASE}/learn/api/public/v1/courses/{course.pk1}/contents/{content_id}/attachments"
                        att_r = api.get(att_url, timeout=30000)
                        if att_r.ok:
                            results = att_r.json().get("results", [])
                            for att in results:
                                att_id = att.get("id")
                                att_name = safe(att.get("fileName") or title)
                                dl_url = f"{BASE}/learn/api/public/v1/courses/{course.pk1}/contents/{content_id}/attachments/{att_id}/download"
                                
                                dest = current_dir / att_name
                                if dest.exists() and dest.stat().st_size > 0:
                                    log(f"      Skip (already exists): {att_name}")
                                else:
                                    log(f"      Downloading [Ultra API]: {att_name} ...")
                                    with DL_SESSION.get(dl_url, stream=True, timeout=60) as dl_r:
                                        if dl_r.ok:
                                            with open(dest, "wb") as f:
                                                for chunk in dl_r.iter_content(chunk_size=1024*1024):
                                                    if chunk: f.write(chunk)
                                            total_files += 1
                                        else:
                                            log(f"      [!] Download failed: HTTP {dl_r.status_code}")
                    except Exception as e:
                        log(f"      [!] Attachment error: {e}")

        walk_ultra(None, course.out_dir, 1)
            
    log(f"  content: {total_folders} folders, {total_files} files")
    summary["folders"] = total_folders
    summary["files"] = total_files
    save(course.out_dir / "_summary.json", json.dumps(summary, indent=2))
    return summary

def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=HEADLESS,
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        try:
            ensure_login(ctx)
            api = ctx.request
            
            for cookie in ctx.cookies():
                DL_SESSION.cookies.set(cookie['name'], cookie['value'])
                
            courses = list_courses(api)
            if not courses:
                log("No courses to archive. Exiting.")
                return 0
            summaries = []
            for c in courses:
                try:
                    s = archive_course(api, c)
                    summaries.append(s)
                except Exception as e:
                    log(f"  !! error on {c.pk1}: {e}")
                    summaries.append({"course_id": c.pk1, "error": str(e)})
            save(OUT / "_index.json", json.dumps(summaries, indent=2))
            log("\nDone.")
            log(f"Output: {OUT}")
            return 0
        finally:
            ctx.close()

if __name__ == "__main__":
    sys.exit(main())