"""
人教社电子教材 WebUI 管理与下载系统 (webui.py)
基于 FastAPI + TailwindCSS 构建，提供与原版网站一致的可视化筛选与一键批量下载体验。
"""

import os
import sys
import re
import time
import json
import asyncio
import webbrowser
import threading
from typing import List, Dict, Optional
from pydantic import BaseModel
import uvicorn
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from pep_core import PepCatalog, PepDownloader, XD_ORDER, XK_ORDER_PREFIX, NJ_ORDER, get_base_dir, normalize_xd

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

app = FastAPI(title="人教社电子教材下载器")

DOWNLOAD_DIR = os.path.join(get_base_dir(), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# 全局任务状态管理
class TaskManager:
    def __init__(self):
        self.queue: List[Dict] = []
        self.is_running = False
        self.current_book: Optional[Dict] = None
        self.current_page = 0
        self.total_pages = 0
        self.status_text = "空闲中"
        self.logs: List[str] = []
        self._lock = threading.Lock()

    def log(self, text: str):
        with self._lock:
            self.logs.append(text)
            if len(self.logs) > 500:
                self.logs.pop(0)

    def get_status(self) -> Dict:
        with self._lock:
            return {
                "is_running": self.is_running,
                "queue_len": len(self.queue),
                "current_book": self.current_book,
                "current_page": self.current_page,
                "total_pages": self.total_pages,
                "progress_percent": int((self.current_page / self.total_pages * 100)) if self.total_pages > 0 else 0,
                "status_text": self.status_text,
                "recent_logs": self.logs[-25:]
            }

    def add_tasks(self, books: List[Dict]):
        with self._lock:
            existing_ids = {b["id"] for b in self.queue}
            if self.current_book:
                existing_ids.add(self.current_book["id"])
            for b in books:
                if b["id"] not in existing_ids:
                    self.queue.append(b)
                    existing_ids.add(b["id"])
        self.log(f"[*] 已添加 {len(books)} 本教材至下载队列。")

    def run_worker(self):
        """后台单线程顺序执行下载队列中的教材"""
        downloader = PepDownloader(headless=True, output_dir=DOWNLOAD_DIR)
        
        while True:
            book_to_download = None
            with self._lock:
                if self.queue:
                    book_to_download = self.queue.pop(0)
                    self.current_book = book_to_download
                    self.is_running = True
                    self.current_page = 0
                    self.total_pages = 0
                    self.status_text = f"正在准备下载《{book_to_download.get('title')}》..."
                else:
                    self.current_book = None
                    self.is_running = False
                    self.current_page = 0
                    self.total_pages = 0
                    self.status_text = "所有任务已完成"

            if not book_to_download:
                time.sleep(1)
                continue

            def progress_callback(cur, total, txt):
                with self._lock:
                    self.current_page = cur
                    self.total_pages = total
                    self.status_text = txt

            def log_callback(txt):
                self.log(txt)

            try:
                xd = normalize_xd(book_to_download.get("xd", "其他学段"))
                nj = book_to_download.get("nj", "通用").strip() or "通用"
                safe_xd = re.sub(r'[\/:*?"<>|]', '_', xd).strip()
                safe_nj = re.sub(r'[\/:*?"<>|]', '_', nj).strip()
                sub_dir = os.path.join(safe_xd, safe_nj)

                self.log(f"==================================================")
                self.log(f"[*] 开始下载教材: [{safe_xd}/{safe_nj}] 《{book_to_download.get('title')}》")
                result = downloader.download_book(
                    book_id=book_to_download["id"],
                    custom_title=book_to_download.get("title"),
                    sub_dir=sub_dir,
                    progress_cb=progress_callback,
                    log_cb=log_callback,
                    skip_if_exists=True,
                    clean_temp=True
                )
                if result:
                    with self._lock:
                        self.status_text = f"已完成：《{book_to_download.get('title')}》"
                else:
                    with self._lock:
                        self.status_text = f"下载失败：《{book_to_download.get('title')}》"
                    self.log(f"[-] 《{book_to_download.get('title')}》未生成 PDF（可能无法读取页数或页面被拦截）")
            except Exception as e:
                with self._lock:
                    self.status_text = f"下载异常：{e}"
                self.log(f"[-] 下载异常: {e}")

            time.sleep(1)


task_manager = TaskManager()

# 启动后台下载线程
worker_thread = threading.Thread(target=task_manager.run_worker, daemon=True)
worker_thread.start()


class FilterQuery(BaseModel):
    xd: Optional[str] = "全部"
    xk: Optional[str] = "全部"
    nj: Optional[str] = "全部"
    keyword: Optional[str] = ""


class BatchDownloadRequest(BaseModel):
    book_ids: List[str]


@app.get("/api/structure")
def get_structure():
    """获取所有学段、学科、年级的结构（保持严格定制排序）"""
    return PepCatalog.get_structure()


@app.post("/api/books")
def list_books(query: FilterQuery):
    """根据条件筛选教材列表"""
    books = PepCatalog.filter_books(
        xd=query.xd,
        xk=query.xk,
        nj=query.nj,
        keyword=query.keyword
    )
    return {"total": len(books), "books": books}


@app.get("/api/status")
def get_download_status():
    """轮询当前下载状态"""
    return task_manager.get_status()


@app.post("/api/download")
def add_download(req: BatchDownloadRequest):
    """提交下载请求"""
    all_books = {b["id"]: b for b in PepCatalog.fetch_and_decrypt_all()}
    selected = [all_books[bid] for bid in req.book_ids if bid in all_books]
    if selected:
        task_manager.add_tasks(selected)
    return {"status": "ok", "added_count": len(selected)}


@app.post("/api/refresh_catalog")
def refresh_catalog():
    """强制重新从官方服务器拉取并解密最新教材目录"""
    try:
        books = PepCatalog.fetch_and_decrypt_all(force_refresh=True)
        return {"status": "ok", "total": len(books)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/clear_cache")
def clear_cache():
    """清理本地临时缓存图片"""
    try:
        res = PepCatalog.clear_cache_files()
        return res
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/open_folder")
def open_download_folder():
    """在资源管理器中打开下载文件夹"""
    try:
        if sys.platform == "win32":
            os.startfile(DOWNLOAD_DIR)
        elif sys.platform == "darwin":
            os.system(f'open "{DOWNLOAD_DIR}"')
        else:
            os.system(f'xdg-open "{DOWNLOAD_DIR}"')
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/", response_class=HTMLResponse)
def index_page():
    """返回一体化前端页面"""
    html_content = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>人教社中小学电子教材下载系统</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .custom-scrollbar::-webkit-scrollbar { width: 6px; height: 6px; }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: #f1f5f9; }
    </style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen flex flex-col font-sans">
    
    <!-- 顶部导航栏 -->
    <header class="bg-white border-b border-slate-200 sticky top-0 z-30 shadow-sm">
        <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
            <div class="flex items-center space-x-3">
                <span class="text-2xl">📚</span>
                <div>
                    <h1 class="text-lg font-bold text-slate-900 leading-tight">人教社电子教材下载器</h1>
                    <p class="text-xs text-slate-500">免滑块验证 · 批量抓取 · 自动合成高清 PDF</p>
                </div>
            </div>
            
            <div class="flex items-center space-x-2">
                <button onclick="clearCache()" id="clearCacheBtn" class="px-3 py-1.5 text-xs font-medium bg-red-50 hover:bg-red-100 text-red-700 rounded-md transition flex items-center space-x-1 border border-red-200" title="删除临时页面下载缓存">
                    <span>🗑️ 清理缓存</span>
                </button>
                <button onclick="refreshCatalog()" id="refreshBtn" class="px-3 py-1.5 text-xs font-medium bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-md transition flex items-center space-x-1 border border-slate-300">
                    <span id="refreshIcon">🔄</span> <span>同步最新目录</span>
                </button>
                <button onclick="openDownloadFolder()" class="px-3 py-1.5 text-xs font-medium bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-md transition flex items-center space-x-1 border border-slate-300">
                    <span>📁 打开保存目录</span>
                </button>
            </div>
        </div>
    </header>

    <!-- 主体区域 -->
    <main class="max-w-7xl mx-auto px-4 py-6 flex-1 w-full space-y-6">
        
        <!-- 任务进度横幅 (有任务时显示) -->
        <div id="progressBanner" class="bg-white border border-blue-200 rounded-xl p-4 shadow-sm space-y-3">
            <div class="flex items-center justify-between">
                <div class="flex items-center space-x-2">
                    <span id="spinner" class="animate-spin text-blue-600 text-lg hidden">⚙️</span>
                    <span class="text-sm font-semibold text-slate-800" id="currentTaskTitle">当前状态: 空闲中</span>
                </div>
                <div class="text-xs text-slate-500">
                    队列等待: <span id="queueCount" class="font-bold text-blue-600">0</span> 本
                </div>
            </div>
            
            <div class="w-full bg-slate-100 rounded-full h-2.5 overflow-hidden">
                <div id="progressBar" class="bg-blue-600 h-2.5 rounded-full transition-all duration-300" style="width: 0%"></div>
            </div>
            
            <div class="flex justify-between text-xs text-slate-500">
                <span id="statusDetail">就绪</span>
                <span id="progressPercent">0%</span>
            </div>
        </div>

        <!-- 筛选与搜索卡片 -->
        <div class="bg-white border border-slate-200 rounded-xl p-5 shadow-sm space-y-4">
            
            <!-- 搜索框 -->
            <div class="relative">
                <input type="text" id="searchInput" placeholder="全局搜索教材（例如：必修一、高一语文、道德与法治...）" 
                       class="w-full pl-10 pr-4 py-2.5 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-transparent transition"
                       oninput="onSearchChange()">
                <span class="absolute left-3.5 top-3 text-slate-400">🔍</span>
            </div>

            <!-- 学段过滤 -->
            <div class="flex items-start space-x-2">
                <span class="text-xs font-semibold text-slate-500 whitespace-nowrap pt-1.5 w-14">学段：</span>
                <div id="xdPills" class="flex flex-wrap gap-1.5">
                    <button class="pill-btn active px-3 py-1 text-xs rounded-md bg-blue-600 text-white" onclick="selectXd('全部')">全部</button>
                </div>
            </div>

            <!-- 学科过滤 -->
            <div class="flex items-start space-x-2 border-t border-slate-100 pt-3">
                <span class="text-xs font-semibold text-slate-500 whitespace-nowrap pt-1.5 w-14">学科：</span>
                <div id="xkPills" class="flex flex-wrap gap-1.5 max-h-24 overflow-y-auto custom-scrollbar">
                    <button class="pill-btn active px-3 py-1 text-xs rounded-md bg-blue-600 text-white" onclick="selectXk('全部')">全部</button>
                </div>
            </div>

            <!-- 年级过滤 -->
            <div class="flex items-start space-x-2 border-t border-slate-100 pt-3">
                <span class="text-xs font-semibold text-slate-500 whitespace-nowrap pt-1.5 w-14">年级：</span>
                <div id="njPills" class="flex flex-wrap gap-1.5">
                    <button class="pill-btn active px-3 py-1 text-xs rounded-md bg-blue-600 text-white" onclick="selectNj('全部')">全部</button>
                </div>
            </div>

        </div>

        <!-- 操作与教材列表 -->
        <div class="space-y-4">
            
            <!-- 批量操作条 -->
            <div class="flex items-center justify-between bg-white border border-slate-200 px-4 py-3 rounded-lg text-xs">
                <div class="flex items-center space-x-4">
                    <label class="flex items-center space-x-2 cursor-pointer select-none">
                        <input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAll()" class="rounded text-blue-600 focus:ring-blue-500">
                        <span class="font-medium text-slate-700">全选当前筛选</span>
                    </label>
                    <span class="text-slate-500">已找到 <b id="totalBooksCount" class="text-slate-900">0</b> 本教材</span>
                    <span class="text-slate-500">已选中 <b id="selectedCount" class="text-blue-600">0</b> 本</span>
                </div>
                
                <button onclick="downloadSelected()" id="batchBtn" disabled
                        class="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed text-white font-medium rounded-md shadow-sm transition flex items-center space-x-1.5">
                    <span>📥 一键批量下载已选教材</span>
                </button>
            </div>

            <!-- 教材卡片网格 -->
            <div id="bookGrid" class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
                <!-- 动态填充卡片 -->
            </div>

        </div>

    </main>

    <script>
        let structure = {};
        let currentXd = "全部";
        let currentXk = "全部";
        let currentNj = "全部";
        let currentSearch = "";
        let currentBooks = [];
        let selectedBookIds = new Set();

        // 页面初始化
        async function init() {
            const res = await fetch('/api/structure');
            structure = await res.json();
            renderFilters();
            await fetchBooks();
            setInterval(pollStatus, 1500);
        }

        function renderFilters() {
            // 渲染学段（后端已严格按规定排序）
            const xdContainer = document.getElementById('xdPills');
            const xds = ["全部", ...Object.keys(structure)];
            xdContainer.innerHTML = xds.map(xd => `
                <button onclick="selectXd('${xd}')" class="pill-xd px-2.5 py-1 text-xs rounded-md border transition ${xd === currentXd ? 'bg-blue-600 border-blue-600 text-white font-medium' : 'bg-slate-100 hover:bg-slate-200 border-slate-200 text-slate-700'}">${xd}</button>
            `).join('');

            // 渲染学科（后端已按规定排序：语文、数学、英语...）
            const xkContainer = document.getElementById('xkPills');
            let subjects = [];
            if (currentXd === "全部") {
                const sSet = new Set();
                Object.values(structure).forEach(item => item.subjects.forEach(s => sSet.add(s)));
                // 保持学科定制优先排序
                const orderPrefix = ['语文', '数学', '英语', '物理', '化学', '历史', '思想政治', '地理', '生物学', '音乐', '道德与法治'];
                subjects = ["全部", ...Array.from(sSet).sort((a, b) => {
                    const ia = orderPrefix.indexOf(a);
                    const ib = orderPrefix.indexOf(b);
                    if (ia !== -1 && ib !== -1) return ia - ib;
                    if (ia !== -1) return -1;
                    if (ib !== -1) return 1;
                    return a.localeCompare(b, 'zh');
                })];
            } else {
                subjects = ["全部", ...(structure[currentXd]?.subjects || [])];
            }
            xkContainer.innerHTML = subjects.map(xk => `
                <button onclick="selectXk('${xk}')" class="pill-xk px-2.5 py-1 text-xs rounded-md border transition ${xk === currentXk ? 'bg-blue-600 border-blue-600 text-white font-medium' : 'bg-slate-100 hover:bg-slate-200 border-slate-200 text-slate-700'}">${xk}</button>
            `).join('');

            // 渲染年级（后端已按规定排序：一年级、二年级...）
            const njContainer = document.getElementById('njPills');
            let grades = [];
            if (currentXd === "全部") {
                const gSet = new Set();
                Object.values(structure).forEach(item => item.grades.forEach(g => gSet.add(g)));
                const orderNj = ['一年级', '二年级', '三年级', '四年级', '五年级', '六年级', '七年级', '八年级', '九年级', '三年级;四年级', '五年级;六年级', '专项', '必修', '选择性必修'];
                grades = ["全部", ...Array.from(gSet).sort((a, b) => {
                    const ia = orderNj.indexOf(a);
                    const ib = orderNj.indexOf(b);
                    if (ia !== -1 && ib !== -1) return ia - ib;
                    if (ia !== -1) return -1;
                    if (ib !== -1) return 1;
                    return a.localeCompare(b, 'zh');
                })];
            } else {
                grades = ["全部", ...(structure[currentXd]?.grades || [])];
            }
            njContainer.innerHTML = grades.map(nj => `
                <button onclick="selectNj('${nj}')" class="pill-nj px-2.5 py-1 text-xs rounded-md border transition ${nj === currentNj ? 'bg-blue-600 border-blue-600 text-white font-medium' : 'bg-slate-100 hover:bg-slate-200 border-slate-200 text-slate-700'}">${nj}</button>
            `).join('');
        }

        async function selectXd(xd) {
            currentXd = xd;
            currentXk = "全部";
            currentNj = "全部";
            renderFilters();
            await fetchBooks();
        }

        async function selectXk(xk) {
            currentXk = xk;
            renderFilters();
            await fetchBooks();
        }

        async function selectNj(nj) {
            currentNj = nj;
            renderFilters();
            await fetchBooks();
        }

        let debounceTimer;
        function onSearchChange() {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                currentSearch = document.getElementById('searchInput').value.trim();
                fetchBooks();
            }, 300);
        }

        async function fetchBooks() {
            const res = await fetch('/api/books', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    xd: currentXd,
                    xk: currentXk,
                    nj: currentNj,
                    keyword: currentSearch
                })
            });
            const data = await res.json();
            currentBooks = data.books;
            document.getElementById('totalBooksCount').innerText = currentBooks.length;
            renderBookGrid();
            updateSelectionUI();
        }

        function renderBookGrid() {
            const grid = document.getElementById('bookGrid');
            if (currentBooks.length === 0) {
                grid.innerHTML = `
                    <div class="col-span-full py-12 text-center text-slate-400 text-sm">
                        没有找到符合条件的教材，请尝试重置筛选条件或更改搜索词。
                    </div>
                `;
                return;
            }

            grid.innerHTML = currentBooks.map(b => {
                const isSelected = selectedBookIds.has(b.id);
                return `
                <div class="bg-white border ${isSelected ? 'border-blue-500 ring-2 ring-blue-100' : 'border-slate-200'} rounded-xl p-3 shadow-sm hover:shadow-md transition flex flex-col justify-between space-y-3 relative group">
                    
                    <!-- 勾选框 -->
                    <div class="absolute top-4 left-4 z-10">
                        <input type="checkbox" ${isSelected ? 'checked' : ''} onchange="toggleBookSelection('${b.id}')"
                               class="w-4 h-4 text-blue-600 rounded border-slate-300 focus:ring-blue-500 cursor-pointer">
                    </div>

                    <div class="space-y-2">
                        <!-- 封面 -->
                        <div class="aspect-[3/4] bg-slate-100 rounded-lg overflow-hidden flex items-center justify-center relative">
                            <img src="${b.thumb || 'https://www.pep.com.cn/images/bg_hp_pep2022.png'}" 
                                 alt="${b.title}" 
                                 class="w-full h-full object-contain p-1"
                                 onerror="this.src='https://jc.pep.com.cn/img/banner_pc.18312502.png'">
                        </div>

                        <!-- 标签 -->
                        <div class="flex flex-wrap gap-1">
                            <span class="px-1.5 py-0.5 bg-blue-50 text-blue-600 text-[10px] font-medium rounded">${b.xd}</span>
                            <span class="px-1.5 py-0.5 bg-emerald-50 text-emerald-600 text-[10px] font-medium rounded">${b.xk}</span>
                            <span class="px-1.5 py-0.5 bg-purple-50 text-purple-600 text-[10px] font-medium rounded">${b.nj}${b.cc || ''}</span>
                        </div>

                        <!-- 标题 -->
                        <h3 class="text-xs font-semibold text-slate-800 line-clamp-2 leading-relaxed" title="${b.title}">
                            ${b.title}
                        </h3>
                    </div>

                    <!-- 操作栏 -->
                    <div class="pt-2 border-t border-slate-100 flex items-center justify-between">
                        <a href="https://book.pep.com.cn/${b.id}/" target="_blank" class="text-xs text-slate-500 hover:text-blue-600 transition">
                            👁️ 在线阅读
                        </a>
                        <button onclick="downloadSingle('${b.id}')" 
                                class="px-2.5 py-1 bg-slate-100 hover:bg-blue-600 hover:text-white text-slate-700 text-xs font-medium rounded transition">
                            📥 下载 PDF
                        </button>
                    </div>

                </div>
                `;
            }).join('');
        }

        function toggleBookSelection(id) {
            if (selectedBookIds.has(id)) selectedBookIds.delete(id);
            else selectedBookIds.add(id);
            renderBookGrid();
            updateSelectionUI();
        }

        function toggleSelectAll() {
            const selectAll = document.getElementById('selectAllCheckbox').checked;
            if (selectAll) {
                currentBooks.forEach(b => selectedBookIds.add(b.id));
            } else {
                currentBooks.forEach(b => selectedBookIds.delete(b.id));
            }
            renderBookGrid();
            updateSelectionUI();
        }

        function updateSelectionUI() {
            const count = selectedBookIds.size;
            document.getElementById('selectedCount').innerText = count;
            document.getElementById('batchBtn').disabled = (count === 0);
            const selectAllCheckbox = document.getElementById('selectAllCheckbox');
            if (currentBooks.length > 0) {
                const allSelected = currentBooks.every(b => selectedBookIds.has(b.id));
                selectAllCheckbox.checked = allSelected;
            } else {
                selectAllCheckbox.checked = false;
            }
        }

        async function downloadSingle(id) {
            try {
                const res = await fetch('/api/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ book_ids: [id] })
                });
                const data = await res.json();
                if (!data.added_count) {
                    alert('未能加入下载队列，请刷新目录后重试。');
                    return;
                }
                pollStatus();
            } catch (e) {
                alert('提交下载失败: ' + e);
            }
        }

        async function downloadSelected() {
            const ids = Array.from(selectedBookIds);
            if (ids.length === 0) return;
            try {
                const res = await fetch('/api/download', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ book_ids: ids })
                });
                const data = await res.json();
                if (!data.added_count) {
                    alert('未能加入下载队列，请刷新目录后重试。');
                    return;
                }
                selectedBookIds.clear();
                updateSelectionUI();
                renderBookGrid();
                pollStatus();
            } catch (e) {
                alert('提交下载失败: ' + e);
            }
        }

        async function refreshCatalog() {
            const btn = document.getElementById('refreshBtn');
            const icon = document.getElementById('refreshIcon');
            icon.classList.add('animate-spin');
            btn.disabled = true;
            try {
                const res = await fetch('/api/refresh_catalog', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'ok') {
                    alert(`✅ 目录同步成功！共获取到 ${data.total} 本教材。`);
                    await init();
                } else {
                    alert('[-] 同步失败: ' + data.message);
                }
            } catch (e) {
                alert('[-] 网络请求异常: ' + e);
            } finally {
                icon.classList.remove('animate-spin');
                btn.disabled = false;
            }
        }

        async function clearCache() {
            if (!confirm('确定要清理本地下载临时缓存（temp_pages）吗？\\n这不会影响已生成的 PDF 文件。')) {
                return;
            }
            try {
                const res = await fetch('/api/clear_cache', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'ok') {
                    alert(`✅ ${data.message}`);
                } else {
                    alert('[-] 清理失败: ' + data.message);
                }
            } catch (e) {
                alert('[-] 清理失败: ' + e);
            }
        }

        async function openDownloadFolder() {
            await fetch('/api/open_folder', { method: 'POST' });
        }

        async function pollStatus() {
            try {
                const res = await fetch('/api/status');
                const st = await res.json();
                
                document.getElementById('queueCount').innerText = st.queue_len;
                document.getElementById('spinner').style.display = st.is_running ? 'inline-block' : 'none';
                
                if (st.current_book) {
                    document.getElementById('currentTaskTitle').innerText = `正在下载: 《${st.current_book.title}》`;
                    document.getElementById('statusDetail').innerText = `${st.status_text} (${st.current_page}/${st.total_pages} 页)`;
                } else {
                    document.getElementById('currentTaskTitle').innerText = `当前状态: ${st.status_text}`;
                    document.getElementById('statusDetail').innerText = st.is_running ? '正在处理...' : '就绪';
                }
                
                document.getElementById('progressBar').style.width = `${st.progress_percent}%`;
                document.getElementById('progressPercent').innerText = `${st.progress_percent}%`;
            } catch (e) {}
        }

        window.onload = init;
    </script>
</body>
</html>
    """
    return html_content


def main():
    import socket
    def is_port_in_use(p):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('127.0.0.1', p)) == 0

    default_port = 8000 if not is_port_in_use(8000) else 8080
    port = int(os.environ.get("PORT", default_port))
    url = f"http://127.0.0.1:{port}"
    print("=" * 65)
    print("      🚀 人教社电子教材 WebUI 服务器正在启动...")
    print(f"      🔗 请在浏览器打开: {url}")
    print("=" * 65)
    
    # 自动打开默认浏览器
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
