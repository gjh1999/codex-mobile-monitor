"""Example: Using the multi-client manager."""
import json
from pathlib import Path
from client_manager import ClientManager


def demo_multi_client():
    """Demonstrate multi-client functionality."""
    root = Path(__file__).parent
    manager = ClientManager(root)

    print("=" * 60)
    print("多客户端监控示例")
    print("=" * 60)

    # Connect to Codex
    print("\n1. 连接到 Codex...")
    if manager.connect('codex'):
        print("   ✓ Codex 已连接")
    else:
        print("   ✗ Codex 连接失败（可能未安装或未配置）")

    # Connect to Claude Code
    print("\n2. 连接到 Claude Code...")
    if manager.connect('claude'):
        print("   ✓ Claude Code 已连接")
    else:
        print("   ✗ Claude Code 连接失败（可能未安装或未配置）")

    # List all connected clients
    print("\n3. 已连接的客户端：")
    clients = manager.list_clients()
    for client in clients:
        status = "✓ 已连接" if client['connected'] else "✗ 未连接"
        print(f"   {client['type'].upper():20} {status}")
        if client.get('error'):
            print(f"      错误: {client['error'][:60]}")

    # Sync threads from all clients
    print("\n4. 从所有客户端同步任务...")
    manager.sync_threads(limit=10)
    all_threads = manager.list_all_threads()
    print(f"   发现 {len(all_threads)} 个任务")

    # Show threads by client
    print("\n5. 按客户端分类的任务：")
    codex_threads = [t for t in all_threads if t.get('client') == 'codex']
    claude_threads = [t for t in all_threads if t.get('client') == 'claude']

    print(f"\n   Codex ({len(codex_threads)} 个):")
    for t in codex_threads[:3]:
        print(f"      - {t.get('title', 'Unknown')[:40]}")
    if len(codex_threads) > 3:
        print(f"      ... 还有 {len(codex_threads) - 3} 个")

    print(f"\n   Claude Code ({len(claude_threads)} 个):")
    for t in claude_threads[:3]:
        print(f"      - {t.get('title', 'Unknown')[:40]}")
    if len(claude_threads) > 3:
        print(f"      ... 还有 {len(claude_threads) - 3} 个")

    # Try a simple call on Codex
    print("\n6. 测试 RPC 调用...")
    try:
        rpc = manager.get_rpc('codex')
        if rpc:
            # This is a test, might fail if not authorized
            print("   ✓ 可以访问 Codex RPC")
        else:
            print("   - Codex 未连接")
    except Exception as e:
        print(f"   ✗ 错误: {e}")

    # Cleanup
    print("\n7. 断开连接...")
    for client_type in ['codex', 'claude']:
        manager.disconnect(client_type)
    print("   完成")

    print("\n" + "=" * 60)


if __name__ == '__main__':
    demo_multi_client()
