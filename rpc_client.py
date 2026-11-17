"""Serialized JSON-RPC transport; business errors do not close live sessions.
Backward compatible wrapper around universal_rpc.
"""
from universal_rpc import CodexRPC, RPCError

# Re-export for backward compatibility
class RPC(CodexRPC):
    """Legacy RPC class - now wraps CodexRPC for backward compatibility."""
    pass
