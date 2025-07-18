"""
RMCP Session - Wraps MCP sessions with reliability features.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, cast

import anyio
from mcp.shared.session import BaseSession
from mcp.types import JSONRPCRequest, JSONRPCResponse

from .types import (
    MessageStatus,
    RMCPConfig,
    RMCPError,
    RMCPMeta,
    RMCPNetworkError,
    RMCPResponse,
    RMCPResult,
    RMCPTimeoutError,
    RequestTracker,
    RetryPolicy,
    TransactionStatus,
)

logger = logging.getLogger(__name__)


class RMCPSession:
    """
    RMCP Session that wraps an existing MCP session with reliability features.
    
    Provides:
    - ACK/NACK guarantees
    - Automatic retry with exponential backoff
    - Request deduplication via idempotency keys
    - Transaction tracking
    - 100% backward compatibility with MCP
    """
    
    def __init__(
        self,
        mcp_session: BaseSession,
        config: Optional[RMCPConfig] = None
    ):
        self.mcp_session = mcp_session
        self.config = config or RMCPConfig()
        self._rmcp_enabled = False
        self._server_capabilities: Dict[str, Any] = {}
        
        # Request tracking
        self._active_requests: Dict[str, RequestTracker] = {}
        self._deduplication_cache: Dict[str, tuple[RMCPResult, datetime]] = {}
        
        # Semaphore for concurrency control
        self._request_semaphore = anyio.Semaphore(self.config.max_concurrent_requests)
        
        logger.info("RMCP session initialized with config: %s", self.config)
    
    async def __aenter__(self) -> "RMCPSession":
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        await self.close()
    
    def _sanitize_error_message(self, error: Exception) -> str:
        """Sanitize error message to prevent information leakage."""
        error_str = str(error)
        
        # Remove potentially sensitive information
        sensitive_patterns = [
            r'password[=:]\s*\S+',
            r'token[=:]\s*\S+', 
            r'key[=:]\s*\S+',
            r'secret[=:]\s*\S+',
            r'auth[=:]\s*\S+',
            r'/Users/[^/\s]+',  # User paths
            r'/home/[^/\s]+',   # User paths
            r'file://[^\s]+',   # File URLs
        ]
        
        import re
        for pattern in sensitive_patterns:
            error_str = re.sub(pattern, '[REDACTED]', error_str, flags=re.IGNORECASE)
        
        # Limit error message length
        if len(error_str) > 200:
            error_str = error_str[:197] + "..."
        
        return error_str
    
    async def initialize(self, **kwargs) -> Any:
        """
        Initialize the session with RMCP capability negotiation.
        """
        # Add RMCP experimental capabilities to initialization
        if "capabilities" not in kwargs:
            kwargs["capabilities"] = {}
        
        if "experimental" not in kwargs["capabilities"]:
            kwargs["capabilities"]["experimental"] = {}
        
        # Advertise RMCP capabilities
        kwargs["capabilities"]["experimental"]["rmcp"] = {
            "version": "0.1.0",
            "features": ["ack", "retry", "idempotency", "transactions"]
        }
        
        logger.debug("Initializing MCP session with RMCP capabilities")
        result = await self.mcp_session.initialize(**kwargs)
        
        # Check if server supports RMCP
        if hasattr(result, "capabilities") and result.capabilities:
            server_caps = result.capabilities
            if hasattr(server_caps, "experimental") and server_caps.experimental:
                self._server_capabilities = server_caps.experimental
                if "rmcp" in self._server_capabilities:
                    self._rmcp_enabled = True
                    logger.info("RMCP enabled - server supports RMCP features")
                else:
                    logger.info("RMCP disabled - server does not support RMCP")
            else:
                logger.info("RMCP disabled - no experimental capabilities from server")
        
        return result
    
    async def call_tool(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]] = None,
        *,
        idempotency_key: Optional[str] = None,
        timeout_ms: Optional[int] = None,
        retry_policy: Optional[RetryPolicy] = None
    ) -> RMCPResult:
        """
        Call a tool with RMCP reliability guarantees.
        
        Args:
            name: Tool name
            arguments: Tool arguments
            idempotency_key: Optional key for deduplication
            timeout_ms: Optional timeout override
            retry_policy: Optional retry policy override
            
        Returns:
            RMCPResult with both tool result and RMCP metadata
            
        Raises:
            ValueError: If input validation fails
        """
        # Input validation
        if not name or not name.strip():
            raise ValueError("Tool name must be a non-empty string")
        
        if not name.replace('_', 'a').replace('-', 'a').isalnum():
            raise ValueError("Tool name must contain only alphanumeric characters, hyphens, and underscores")
        
        if arguments is not None and not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be a dictionary or None")
        
        if idempotency_key is not None and (not idempotency_key or not idempotency_key.strip()):
            raise ValueError("Idempotency key must be a non-empty string if provided")
        
        if timeout_ms is not None and (timeout_ms <= 0 or timeout_ms > 600000):  # Max 10 minutes
            raise ValueError("Timeout must be between 1ms and 600,000ms (10 minutes)")
        
        # Use provided or default retry policy
        effective_retry_policy = retry_policy or self.config.retry_policy
        effective_timeout = timeout_ms or self.config.default_timeout_ms
        
        # Check deduplication cache first
        if idempotency_key:
            cached_result = self._get_cached_result(idempotency_key)
            if cached_result:
                logger.debug("Returning cached result for idempotency key: %s", idempotency_key)
                return cached_result
        
        # Acquire semaphore for concurrency control
        async with self._request_semaphore:
            return await self._call_tool_with_retry(
                name,
                arguments,
                idempotency_key,
                effective_timeout,
                effective_retry_policy
            )
    
    async def _call_tool_with_retry(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]],
        idempotency_key: Optional[str],
        timeout_ms: int,
        retry_policy: RetryPolicy
    ) -> RMCPResult:
        """Execute tool call with retry logic."""
        rmcp_meta = RMCPMeta(
            idempotency_key=idempotency_key,
            timeout_ms=timeout_ms
        )
        
        # Track request
        tracker = RequestTracker(
            request_id=rmcp_meta.request_id,
            transaction_id=rmcp_meta.transaction_id,
            status=MessageStatus.PENDING,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        self._active_requests[rmcp_meta.request_id] = tracker
        
        last_error: Optional[Exception] = None
        
        try:
            for attempt in range(retry_policy.max_attempts):
                rmcp_meta.retry_count = attempt
                tracker.attempts = attempt + 1
                
                try:
                    tracker.update_status(MessageStatus.SENT)
                    logger.debug(
                        "Attempting tool call %s (attempt %d/%d)",
                        name, attempt + 1, retry_policy.max_attempts
                    )
                    
                    # Call tool with timeout
                    result = await self._execute_tool_call(
                        name, arguments, rmcp_meta, timeout_ms
                    )
                    
                    # Success - update tracker and cache result
                    tracker.update_status(MessageStatus.ACKNOWLEDGED)
                    
                    rmcp_result = RMCPResult(
                        result=result,
                        rmcp_meta=RMCPResponse(
                            ack=True,
                            processed=True,
                            duplicate=False,
                            attempts=attempt + 1,
                            final_status="completed"
                        )
                    )
                    
                    # Cache result if idempotency key provided
                    if idempotency_key:
                        self._cache_result(idempotency_key, rmcp_result)
                    
                    return rmcp_result
                
                except Exception as e:
                    last_error = e
                    tracker.update_status(MessageStatus.FAILED, self._sanitize_error_message(e))
                    
                    logger.warning(
                        "Tool call attempt %d/%d failed: %s",
                        attempt + 1, retry_policy.max_attempts, str(e)
                    )
                    
                    # Check if we should retry
                    if attempt < retry_policy.max_attempts - 1:
                        if self._should_retry(e, retry_policy):
                            delay = self._calculate_retry_delay(
                                attempt, retry_policy
                            )
                            logger.debug("Retrying in %d ms", delay)
                            await asyncio.sleep(delay / 1000.0)
                            continue
                        else:
                            logger.debug("Error not retryable: %s", str(e))
                            break
            
            # All retries exhausted
            tracker.update_status(MessageStatus.FAILED, self._sanitize_error_message(last_error) if last_error else "Unknown error")
        
        finally:
            # Always clean up tracker regardless of success or failure
            self._active_requests.pop(rmcp_meta.request_id, None)
        
        # Return failure result
        error_code = getattr(last_error, "error_code", "UNKNOWN_ERROR")
        sanitized_error = self._sanitize_error_message(last_error) if last_error else "Unknown error"
        
        return RMCPResult(
            result=None,
            rmcp_meta=RMCPResponse(
                ack=False,
                processed=False,
                duplicate=False,
                attempts=retry_policy.max_attempts,
                final_status="failed",
                error_code=error_code,
                error_message=sanitized_error
            )
        )
    
    async def _execute_tool_call(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]],
        rmcp_meta: RMCPMeta,
        timeout_ms: int
    ) -> Any:
        """Execute the actual tool call with RMCP metadata."""
        if not self._rmcp_enabled:
            # Fallback to standard MCP
            return await self._execute_standard_mcp_call(name, arguments, timeout_ms)
        
        # Enhanced MCP call with RMCP metadata
        request = {
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {},
                "_meta": {
                    "rmcp": rmcp_meta.to_dict()
                }
            }
        }
        
        # Execute with timeout
        try:
            with anyio.move_on_after(timeout_ms / 1000.0) as cancel_scope:
                response = await self.mcp_session.send_request(request)
                
                if cancel_scope.cancelled_caught:
                    raise RMCPTimeoutError(
                        f"Tool call timed out after {timeout_ms}ms",
                        timeout_ms
                    )
                
                return response
                
        except Exception as e:
            # Wrap network errors
            if "connection" in str(e).lower() or "network" in str(e).lower():
                raise RMCPNetworkError(f"Network error during tool call: {str(e)}", e)
            raise
    
    async def _execute_standard_mcp_call(
        self,
        name: str,
        arguments: Optional[Dict[str, Any]],
        timeout_ms: int
    ) -> Any:
        """Execute standard MCP tool call without RMCP enhancements."""
        request = {
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments or {}
            }
        }
        
        with anyio.move_on_after(timeout_ms / 1000.0) as cancel_scope:
            response = await self.mcp_session.send_request(request)
            
            if cancel_scope.cancelled_caught:
                raise RMCPTimeoutError(
                    f"Tool call timed out after {timeout_ms}ms",
                    timeout_ms
                )
            
            return response
    
    def _should_retry(self, error: Exception, retry_policy: RetryPolicy) -> bool:
        """Determine if an error should trigger a retry."""
        if isinstance(error, RMCPError):
            return error.retryable
        
        error_str = str(error).upper()
        return any(
            retryable_error in error_str
            for retryable_error in retry_policy.retryable_errors
        )
    
    def _calculate_retry_delay(self, attempt: int, retry_policy: RetryPolicy) -> int:
        """Calculate delay for retry attempt with exponential backoff and jitter."""
        delay = min(
            retry_policy.base_delay_ms * (retry_policy.backoff_multiplier ** attempt),
            retry_policy.max_delay_ms
        )
        
        if retry_policy.jitter:
            # Add ±20% jitter
            jitter = delay * 0.2 * (random.random() * 2 - 1)
            delay = int(delay + jitter)
        
        # Ensure delay is always positive and within bounds
        return max(delay, retry_policy.base_delay_ms)
    
    def _get_cached_result(self, idempotency_key: str) -> Optional[RMCPResult]:
        """Get cached result for idempotency key."""
        if idempotency_key in self._deduplication_cache:
            cached_result, timestamp = self._deduplication_cache[idempotency_key]
            
            # Check if cache entry is still valid
            current_time = datetime.utcnow()
            cutoff_time = current_time - timedelta(milliseconds=self.config.deduplication_window_ms)
            
            if timestamp >= cutoff_time:
                # Mark as duplicate and return
                cached_result.rmcp_meta.duplicate = True
                return cached_result
            else:
                # Entry expired, remove it
                del self._deduplication_cache[idempotency_key]
        
        return None
    
    def _cache_result(self, idempotency_key: str, result: RMCPResult) -> None:
        """Cache result for deduplication with time-based eviction."""
        current_time = datetime.utcnow()
        self._deduplication_cache[idempotency_key] = (result, current_time)
        
        # Clean up expired entries
        cutoff_time = current_time - timedelta(milliseconds=self.config.deduplication_window_ms)
        expired_keys = [
            key for key, (_, timestamp) in self._deduplication_cache.items()
            if timestamp < cutoff_time
        ]
        for key in expired_keys:
            del self._deduplication_cache[key]
        
        # Additional safety: if cache grows too large, remove oldest entries
        if len(self._deduplication_cache) > 1000:
            # Sort by timestamp and remove oldest 100 entries
            sorted_entries = sorted(
                self._deduplication_cache.items(), 
                key=lambda x: x[1][1]  # Sort by timestamp
            )
            for key, _ in sorted_entries[:100]:
                del self._deduplication_cache[key]
    
    @property
    def rmcp_enabled(self) -> bool:
        """Whether RMCP features are enabled for this session."""
        return self._rmcp_enabled
    
    @property
    def active_requests(self) -> Dict[str, RequestTracker]:
        """Currently active RMCP requests."""
        return self._active_requests.copy()
    
    async def close(self) -> None:
        """Close the RMCP session and underlying MCP session."""
        logger.info("Closing RMCP session")
        
        # Wait for active requests to complete or timeout
        if self._active_requests:
            logger.info("Waiting for %d active requests to complete", len(self._active_requests))
            # Give requests a chance to complete
            await asyncio.sleep(0.1)
        
        # Close underlying MCP session
        if hasattr(self.mcp_session, "close"):
            await self.mcp_session.close()
        
        # Clear caches
        self._active_requests.clear()
        self._deduplication_cache.clear()
        
        logger.info("RMCP session closed")