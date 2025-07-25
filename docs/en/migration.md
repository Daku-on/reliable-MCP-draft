# Migration from MCP to MCP-Tx

Step-by-step guide to upgrade from standard MCP to MCP-Tx with reliability guarantees.

## Why Migrate to MCP-Tx?

**Standard MCP Limitations**:
- ❌ No delivery guarantees (fire-and-forget)
- ❌ No automatic retry on failures
- ❌ No duplicate request protection
- ❌ Limited error handling and recovery
- ❌ No request lifecycle visibility

**MCP-Tx Benefits**:
- ✅ **Guaranteed delivery** with ACK/NACK
- ✅ **Automatic retry** with exponential backoff
- ✅ **Idempotency** prevents duplicate execution
- ✅ **Rich error handling** with detailed context
- ✅ **Request tracking** and transaction support
- ✅ **100% backward compatible** with existing MCP servers

## Migration Strategies

### Strategy 1: Drop-in Replacement (Recommended)

**Best for**: Applications that want immediate reliability benefits with minimal code changes.

#### Before (Standard MCP)
```python
import asyncio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioClientTransport

async def mcp_example():
    # Standard MCP setup
    transport = StdioClientTransport(...)
    session = ClientSession(transport)
    
    await session.initialize()
    
    # Standard tool call - no guarantees
    try:
        result = await session.call_tool("file_reader", {"path": "/data.txt"})
        print(f"Result: {result}")
    except Exception as e:
        print(f"Error: {e}")
        # Manual retry logic required
    
    await session.close()
```

#### After (MCP-Tx Wrapper)
```python
import asyncio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioClientTransport
from mcp_tx import MCPTxSession  # Add MCP-Tx import

async def rmcp_example():
    # Same MCP setup
    transport = StdioClientTransport(...)
    mcp_session = ClientSession(transport)
    
    # Wrap with MCP-Tx for reliability
    rmcp_session = MCPTxSession(mcp_session)
    
    await rmcp_session.initialize()  # Same interface
    
    # Enhanced tool call with guarantees
    result = await rmcp_session.call_tool("file_reader", {"path": "/data.txt"})
    
    # Rich reliability information
    print(f"Acknowledged: {result.ack}")
    print(f"Processed: {result.processed}")
    print(f"Attempts: {result.attempts}")
    print(f"Status: {result.final_status}")
    
    if result.ack:
        print(f"Result: {result.result}")
    else:
        print(f"Failed: {result.rmcp_meta.error_message}")
    
    await rmcp_session.close()
```

**Migration Steps**:
1. ✅ Install MCP-Tx: `uv add mcp_tx`
2. ✅ Import MCPTxSession: `from mcp_tx import MCPTxSession`
3. ✅ Wrap MCP session: `rmcp_session = MCPTxSession(mcp_session)`
4. ✅ Update result handling: Use `result.ack` and `result.result`
5. ✅ Test with existing servers (automatic fallback if no MCP-Tx support)

### Strategy 2: Gradual Enhancement

**Best for**: Large applications that want to add MCP-Tx features incrementally.

#### Phase 1: Basic Wrapper
```python
class ApplicationClient:
    def __init__(self, mcp_session):
        # Phase 1: Wrap with basic MCP-Tx
        self.session = MCPTxSession(mcp_session)
        self.initialized = False
    
    async def initialize(self):
        await self.session.initialize()
        self.initialized = True
    
    async def read_file(self, path: str) -> str:
        """Phase 1: Basic reliability."""
        if not self.initialized:
            raise RuntimeError("Client not initialized")
        
        result = await self.session.call_tool("file_reader", {"path": path})
        
        if not result.ack:
            raise RuntimeError(f"File read failed: {result.rmcp_meta.error_message}")
        
        return result.result["content"]
```

#### Phase 2: Add Idempotency
```python
    async def write_file(self, path: str, content: str) -> bool:
        """Phase 2: Add idempotency for write operations."""
        import hashlib
        
        # Create deterministic idempotency key
        content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
        idempotency_key = f"write-{path.replace('/', '_')}-{content_hash}"
        
        result = await self.session.call_tool(
            "file_writer",
            {"path": path, "content": content},
            idempotency_key=idempotency_key
        )
        
        return result.ack
```

#### Phase 3: Custom Retry Policies
```python
    async def api_call(self, endpoint: str, data: dict = None) -> dict:
        """Phase 3: Custom retry for external APIs."""
        from mcp_tx import RetryPolicy
        
        # Aggressive retry for external APIs
        api_retry = RetryPolicy(
            max_attempts=5,
            base_delay_ms=1000,
            backoff_multiplier=2.0,
            jitter=True,
            retryable_errors=["CONNECTION_ERROR", "TIMEOUT", "RATE_LIMITED"]
        )
        
        result = await self.session.call_tool(
            "http_client",
            {"endpoint": endpoint, "data": data},
            retry_policy=api_retry,
            timeout_ms=30000
        )
        
        if not result.ack:
            raise RuntimeError(f"API call failed: {result.rmcp_meta.error_message}")
        
        return result.result
```

### Strategy 3: Feature Flag Approach

**Best for**: Production systems that need gradual rollout with rollback capability.

```python
import os
from typing import Union
from mcp.client.session import ClientSession
from mcp_tx import MCPTxSession

class ConfigurableClient:
    def __init__(self, mcp_session: ClientSession):
        self.mcp_session = mcp_session
        
        # Feature flag for MCP-Tx
        use_rmcp = os.getenv("USE_MCP-Tx", "false").lower() == "true"
        
        if use_rmcp:
            print("🚀 Using MCP-Tx for enhanced reliability")
            self.session = MCPTxSession(mcp_session)
        else:
            print("📡 Using standard MCP")
            self.session = mcp_session
        
        self.is_rmcp = isinstance(self.session, MCPTxSession)
    
    async def call_tool_with_fallback(self, name: str, arguments: dict) -> dict:
        """Call tool with MCP-Tx if available, fallback to MCP error handling."""
        
        if self.is_rmcp:
            # MCP-Tx path - rich error handling
            result = await self.session.call_tool(name, arguments)
            
            if result.ack:
                return {
                    "success": True,
                    "data": result.result,
                    "metadata": {
                        "attempts": result.attempts,
                        "status": result.final_status
                    }
                }
            else:
                return {
                    "success": False,
                    "error": result.rmcp_meta.error_message,
                    "attempts": result.attempts
                }
        else:
            # Standard MCP path - basic error handling
            try:
                result = await self.session.call_tool(name, arguments)
                return {
                    "success": True,
                    "data": result,
                    "metadata": {"attempts": 1}
                }
            except Exception as e:
                return {
                    "success": False,
                    "error": str(e),
                    "attempts": 1
                }
```

## Common Migration Patterns

### 1. Error Handling Migration

#### Before (MCP)
```python
# Basic try-catch with manual retry
async def unreliable_operation():
    max_retries = 3
    delay = 1.0
    
    for attempt in range(max_retries):
        try:
            result = await mcp_session.call_tool("unreliable_api", {})
            return result
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(delay)
                delay *= 2  # Manual exponential backoff
            else:
                raise e
```

#### After (MCP-Tx)
```python
# Automatic retry with rich error handling
async def unreliable_operation():
    from rmcp.types import MCP-TxTimeoutError, MCP-TxNetworkError
    
    try:
        result = await rmcp_session.call_tool("unreliable_api", {})
        return result.result if result.ack else None
    except MCP-TxTimeoutError as e:
        print(f"Operation timed out after {e.details['timeout_ms']}ms")
        return None
    except MCP-TxNetworkError as e:
        print(f"Network error: {e.message}")
        return None
```

### 2. Idempotency Migration

#### Before (MCP)
```python
# Manual duplicate detection
processed_operations = set()

async def idempotent_operation(operation_id: str, data: dict):
    if operation_id in processed_operations:
        print(f"Operation {operation_id} already processed")
        return
    
    try:
        result = await mcp_session.call_tool("processor", {"id": operation_id, "data": data})
        processed_operations.add(operation_id)
        return result
    except Exception as e:
        # Don't mark as processed on failure
        raise e
```

#### After (MCP-Tx)
```python
# Automatic duplicate detection
async def idempotent_operation(operation_id: str, data: dict):
    result = await rmcp_session.call_tool(
        "processor",
        {"id": operation_id, "data": data},
        idempotency_key=f"process-{operation_id}"
    )
    
    if result.rmcp_meta.duplicate:
        print(f"Operation {operation_id} already processed")
    
    return result.result if result.ack else None
```

### 3. Configuration Migration

#### Before (MCP)
```python
# Application-level configuration
class MCPClient:
    def __init__(self):
        self.timeout = 30.0
        self.max_retries = 3
        self.retry_delay = 1.0
        
        # Manual implementation of reliability features
```

#### After (MCP-Tx)
```python
# Declarative MCP-Tx configuration
from mcp_tx import MCPTxConfig, RetryPolicy

class MCP-TxClient:
    def __init__(self):
        config = MCPTxConfig(
            default_timeout_ms=30000,
            retry_policy=RetryPolicy(
                max_attempts=3,
                base_delay_ms=1000,
                backoff_multiplier=2.0,
                jitter=True
            ),
            max_concurrent_requests=10,
            deduplication_window_ms=300000
        )
        
        self.session = MCPTxSession(mcp_session, config)
        # Reliability features handled automatically
```

## Migration Checklist

### Pre-Migration Assessment

- [ ] **Inventory MCP usage**: Document all `call_tool()` calls in codebase
- [ ] **Identify critical operations**: Mark operations that need reliability guarantees
- [ ] **Review error handling**: Document current error handling patterns
- [ ] **Check MCP server versions**: Ensure compatibility with MCP-Tx
- [ ] **Plan testing strategy**: Define test scenarios for migration validation

### Migration Execution

- [ ] **Install MCP-Tx**: `uv add mcp_tx`
- [ ] **Update imports**: Add `from mcp_tx import MCPTxSession`
- [ ] **Wrap MCP sessions**: Replace direct MCP usage with MCP-Tx wrapper
- [ ] **Update result handling**: Use `result.ack` and `result.result` pattern
- [ ] **Configure MCP-Tx**: Set appropriate timeouts, retry policies, concurrency limits
- [ ] **Add idempotency keys**: For operations that should be idempotent
- [ ] **Enhance error handling**: Use MCP-Tx-specific exception types

### Post-Migration Validation

- [ ] **Test backward compatibility**: Verify works with non-MCP-Tx servers
- [ ] **Validate reliability features**: Test retry, idempotency, timeout handling
- [ ] **Performance testing**: Measure MCP-Tx overhead vs standard MCP
- [ ] **Monitor error rates**: Compare error rates before/after migration
- [ ] **Update documentation**: Document new MCP-Tx-specific features

## Troubleshooting Migration Issues

### Issue: Import Errors

```python
# ❌ Problem
from mcp_tx import MCPTxSession  # ModuleNotFoundError

# ✅ Solution  
# Install MCP-Tx first
# uv add mcp_tx
# or pip install mcp_tx
```

### Issue: Result Access Errors

```python
# ❌ Problem
result = await rmcp_session.call_tool("test", {})
print(result)  # MCP-TxResult object, not direct result

# ✅ Solution
result = await rmcp_session.call_tool("test", {})
if result.ack:
    print(result.result)  # Access actual result
else:
    print(f"Failed: {result.rmcp_meta.error_message}")
```

### Issue: Server Compatibility

```python
# ❌ Problem
# Server doesn't support MCP-Tx experimental capabilities

# ✅ Solution - Automatic fallback
rmcp_session = MCPTxSession(mcp_session)
await rmcp_session.initialize()

if rmcp_session.rmcp_enabled:
    print("✅ MCP-Tx features active")
else:
    print("⚠️ Falling back to standard MCP")
    # MCP-Tx still works, just without server-side features
```

### Issue: Performance Concerns

```python
# ❌ Problem
# MCP-Tx adds overhead for simple operations

# ✅ Solution - Selective usage
class HybridClient:
    def __init__(self, mcp_session):
        self.mcp_session = mcp_session
        self.rmcp_session = MCPTxSession(mcp_session)
    
    async def simple_call(self, tool: str, args: dict):
        # Use MCP for simple, non-critical operations
        return await self.mcp_session.call_tool(tool, args)
    
    async def critical_call(self, tool: str, args: dict):
        # Use MCP-Tx for critical operations needing reliability
        result = await self.rmcp_session.call_tool(tool, args)
        return result.result if result.ack else None
```

## Best Practices for Migration

### 1. Start Small
- Begin with non-critical operations
- Gradually expand to critical systems
- Use feature flags for easy rollback

### 2. Preserve Existing APIs
```python
# ✅ Good: Maintain existing function signatures
async def read_file(path: str) -> str:
    result = await rmcp_session.call_tool("file_reader", {"path": path})
    
    if not result.ack:
        raise RuntimeError(f"File read failed: {result.rmcp_meta.error_message}")
    
    return result.result["content"]

# ❌ Bad: Force callers to handle MCP-Tx details
async def read_file(path: str) -> MCP-TxResult:
    return await rmcp_session.call_tool("file_reader", {"path": path})
```

### 3. Leverage MCP-Tx Features Gradually
1. **Phase 1**: Basic wrapper (automatic retry, error handling)
2. **Phase 2**: Add idempotency for write operations
3. **Phase 3**: Custom retry policies for different operation types
4. **Phase 4**: Advanced features (transactions, monitoring)

### 4. Monitor and Measure
- Track reliability metrics (success rates, retry counts)
- Monitor performance impact (latency, throughput)
- Compare error rates before/after migration
- Set up alerts for MCP-Tx-specific issues

---

**Next**: [FAQ](faq.md) | **Previous**: [Examples](examples/basic.md)