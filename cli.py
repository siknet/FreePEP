"""
人教社电子教材交互式终端下载器 (cli.py)
支持：
1. 交互式菜单筛选（学段 -> 学科 -> 年级）
2. 批量全选下载或勾选指定教材下载
3. 命令行参数一键过滤下载 (例如: python cli.py --xd "小学（六三学制）" --xk "语文" --nj "一年级")
4. 关键词全局模糊搜索下载
5. 缓存清理与目录强制刷新
"""

import sys
import os
import re
import argparse
from typing import List, Dict
from pep_core import PepCatalog, PepDownloader, XD_ORDER, XK_ORDER_PREFIX, NJ_ORDER, sort_xk_key, sort_nj_key

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def print_banner():
    print("=" * 65)
    print("      📚 人民教育出版社 (PEP) 电子教材终端下载工具")
    print("=" * 65)


def select_from_list(prompt: str, options: List[str], allow_all: bool = True) -> str:
    """辅助函数：提供多选编号菜单"""
    items = ["全部"] + options if allow_all and "全部" not in options else options
    print(f"\n👉 请选择【{prompt}】：")
    for i, item in enumerate(items, 1):
        print(f"  [{i}] {item}")
    
    while True:
        try:
            choice = input(f"请输入序号 (1-{len(items)}, 默认 1): ").strip()
            if not choice:
                return items[0]
            idx = int(choice)
            if 1 <= idx <= len(items):
                return items[idx - 1]
            print(f"[-] 输入超出范围，请输入 1 到 {len(items)} 之间的数字。")
        except ValueError:
            print("[-] 请输入有效的数字序号。")


def interactive_mode():
    """交互式导航筛选模式"""
    print_banner()
    print("[*] 正在加载教材分类结构...")
    structure = PepCatalog.get_structure()
    all_xds = list(structure.keys())

    print("\n请选择检索模式：")
    print("  [1] 分类层级筛选（学段 ➔ 学科 ➔ 年级）")
    print("  [2] 关键词全局搜索（如输入：'必修一'、'高一数学'、'生物'）")
    print("  [3] 一键按「学段 ➔ 年级」两层目录全量下载全部教材")
    
    mode_choice = input("请输入模式编号 (1/2/3, 默认 1): ").strip()
    
    matched_books = []
    
    if mode_choice == "3":
        matched_books = PepCatalog.fetch_and_decrypt_all()
        print(f"\n[+] 已加载全网全部教材，共 {len(matched_books)} 本。")
    elif mode_choice == "2":
        kw = input("\n🔍 请输入搜索关键词: ").strip()
        if not kw:
            print("[-] 关键词不能为空！")
            return
        matched_books = PepCatalog.filter_books(keyword=kw)
    else:
        # 1. 选择学段（已按规定排序）
        selected_xd = select_from_list("学段", all_xds, allow_all=True)
        
        # 获取该学段下的学科和年级
        if selected_xd == "全部":
            s_set = set(s for x in structure.values() for s in x["subjects"])
            all_subjects = sorted(list(s_set), key=sort_xk_key)
            g_set = set(g for x in structure.values() for g in x["grades"])
            all_grades = sorted(list(g_set), key=sort_nj_key)
        else:
            all_subjects = structure[selected_xd]["subjects"]
            all_grades = structure[selected_xd]["grades"]
            
        # 2. 选择学科（已按规定排序）
        selected_xk = select_from_list("学科", all_subjects, allow_all=True)
        
        # 3. 选择年级（已按规定排序）
        selected_nj = select_from_list("年级", all_grades, allow_all=True)
        
        matched_books = PepCatalog.filter_books(xd=selected_xd, xk=selected_xk, nj=selected_nj)

    if not matched_books:
        print("\n[-] 未匹配到任何符合条件的教材！")
        return

    print(f"\n✅ 共检索到 {len(matched_books)} 本教材：")
    print("-" * 65)
    for idx, b in enumerate(matched_books[:15], 1):
        xd = b.get("xd", "")
        xk = b.get("xk", "")
        nj = b.get("nj", "")
        cc = b.get("cc", "")
        title = b.get("title", "")
        print(f"  [{idx:2d}] [{xd}|{xk}|{nj}{cc}] 《{title}》 (ID: {b['id']})")
    if len(matched_books) > 15:
        print(f"  ... 以及其余 {len(matched_books) - 15} 本教材（已折叠显示）")
    print("-" * 65)

    print("\n请选择下载范围：")
    print("  • 直接按 Enter 或输入 'all': 下载当前列表中的【全部】教材")
    print("  • 输入单个序号（如 '3'）: 只下载第 3 本")
    print("  • 输入多个序号（如 '1,3,5' 或范围 '1-4'）: 批量下载指定教材")
    print("  • 输入 'q': 退出")

    select_str = input("\n请输入下载指令: ").strip().lower()
    if select_str == "q":
        print("[*] 已取消操作。")
        return

    to_download = []
    if not select_str or select_str == "all":
        to_download = matched_books
    else:
        indices = set()
        for part in select_str.split(","):
            part = part.strip()
            if "-" in part:
                try:
                    s, e = map(int, part.split("-"))
                    for i in range(s, e + 1):
                        indices.add(i)
                except ValueError:
                    pass
            elif part.isdigit():
                indices.add(int(part))
                
        for i in sorted(list(indices)):
            if 1 <= i <= len(matched_books):
                to_download.append(matched_books[i - 1])

    if not to_download:
        print("[-] 未选择有效教材，退出。")
        return

    out_dir = os.path.abspath("./downloads")
    print(f"\n🚀 即将开始下载 {len(to_download)} 本教材，基础保存目录: {out_dir}")
    print(f"🌲 采用「学段 ➔ 年级」两层子目录分类保存 (已存在 PDF 自动跳过)")
    downloader = PepDownloader(headless=True, output_dir=out_dir)

    for idx, b in enumerate(to_download, 1):
        xd = b.get("xd", "其他学段")
        nj = b.get("nj", "通用").strip() or "通用"
        import re
        safe_xd = re.sub(r'[\/:*?"<>|]', '_', xd).strip()
        safe_nj = re.sub(r'[\/:*?"<>|]', '_', nj).strip()
        sub_dir = os.path.join(safe_xd, safe_nj)

        print(f"\n==================================================")
        print(f"[{idx}/{len(to_download)}] [{safe_xd}/{safe_nj}] 《{b.get('title')}》")
        print(f"==================================================")
        downloader.download_book(
            book_id=b["id"],
            custom_title=b.get("title"),
            sub_dir=sub_dir,
            skip_if_exists=True,
            clean_temp=True
        )

    print("\n🎉 全部选定任务执行完毕！")


def cli_args_mode(args):
    """命令行参数直接执行模式"""
    print_banner()
    if args.all:
        matched = PepCatalog.fetch_and_decrypt_all()
    else:
        matched = PepCatalog.filter_books(xd=args.xd, xk=args.xk, nj=args.nj, keyword=args.search)

    if not matched:
        print("[-] 未查找到符合条件的教材！")
        return

    print(f"[+] 符合条件的教材共 {len(matched)} 本：")
    for idx, b in enumerate(matched[:15], 1):
        print(f"  [{idx}] [{b.get('xd')}|{b.get('nj')}] 《{b.get('title')}》 (ID: {b['id']})")
    if len(matched) > 15:
        print(f"  ... 以及其余 {len(matched) - 15} 本教材（已折叠）")

    if not args.yes:
        confirm = input(f"\n确认下载以上 {len(matched)} 本教材吗？(y/n, 默认 y): ").strip().lower()
        if confirm == "n":
            print("[*] 已取消。")
            return

    out_dir = os.path.abspath(args.output)
    downloader = PepDownloader(headless=True, output_dir=out_dir)
    use_tree = not args.flat

    print(f"\n📂 保存根目录: {out_dir}")
    print(f"🌲 目录结构: {'按「学段/年级」两层子目录' if use_tree else '全部平铺在根目录'}")

    for idx, b in enumerate(matched, 1):
        sub_dir = None
        if use_tree:
            import re
            safe_xd = re.sub(r'[\/:*?"<>|]', '_', b.get("xd", "其他学段")).strip()
            safe_nj = re.sub(r'[\/:*?"<>|]', '_', b.get("nj", "通用") or "通用").strip()
            sub_dir = os.path.join(safe_xd, safe_nj)

        print(f"\n[{idx}/{len(matched)}] 正在下载: 《{b.get('title')}》...")
        downloader.download_book(
            book_id=b["id"],
            custom_title=b.get("title"),
            sub_dir=sub_dir,
            skip_if_exists=True,
            clean_temp=True
        )

    print(f"\n🎉 下载完成！文件已保存至: {out_dir}")


def main():
    parser = argparse.ArgumentParser(description="人民教育出版社电子教材 CLI 下载器")
    parser.add_argument("--all", "-a", action="store_true", help="下载全网全部 780+ 本教材")
    parser.add_argument("--xd", help="指定学段（如：小学（六三学制）、初中（六三学制）、高中等）")
    parser.add_argument("--xk", help="指定学科（如：语文、数学、英语、物理等）")
    parser.add_argument("--nj", help="指定年级（如：一年级、七年级、必修等）")
    parser.add_argument("--search", "-s", help="全局搜索关键词")
    parser.add_argument("--output", "-o", default="./downloads", help="PDF 文件保存目录 (默认: ./downloads)")
    parser.add_argument("--flat", action="store_true", help="平铺存放在根目录下（默认自动按「学段/年级」两层子目录分类）")
    parser.add_argument("--yes", "-y", action="store_true", help="免确认直接开始下载")
    parser.add_argument("--refresh", "-r", action="store_true", help="强制从官方服务器重新拉取并解密最新教材目录")
    parser.add_argument("--clear-cache", "-c", action="store_true", help="清理本地临时下载缓存 (temp_pages)")

    args = parser.parse_args()

    if args.clear_cache:
        print_banner()
        res = PepCatalog.clear_cache_files()
        print(f"[✔] {res['message']}")
        return

    if args.refresh:
        print_banner()
        print("[*] 正在从人教社官方前端拉取并解密最新教材目录...")
        books = PepCatalog.fetch_and_decrypt_all(force_refresh=True)
        print(f"[+] 目录同步成功！共获取到 {len(books)} 本教材。")
        return

    if args.all or args.xd or args.xk or args.nj or args.search:
        cli_args_mode(args)
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
