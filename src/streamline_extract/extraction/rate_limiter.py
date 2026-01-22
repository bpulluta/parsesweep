"""Rate limiting and retry logic for API calls."""

import time
import logging
from typing import Callable, Any, Optional
from functools import wraps

logger = logging.getLogger(__name__)


class RateLimitHandler:
    """
    Handle API rate limits with exponential backoff and intelligent retry.
    
    Features:
    - Exponential backoff with jitter
    - Rate limit detection from error messages
    - Adaptive delays based on quota resets
    - Request pacing to prevent hitting limits
    """
    
    def __init__(
        self,
        initial_retry_delay: float = 2.0,
        max_retry_delay: float = 120.0,
        max_retries: int = 5,
        backoff_factor: float = 2.0,
        requests_per_minute: Optional[int] = None,
    ):
        """
        Initialize rate limit handler.
        
        Args:
            initial_retry_delay: Initial delay in seconds before first retry
            max_retry_delay: Maximum delay in seconds between retries
            max_retries: Maximum number of retry attempts
            backoff_factor: Multiplier for exponential backoff
            requests_per_minute: Optional rate limit for proactive pacing
        """
        self.initial_retry_delay = initial_retry_delay
        self.max_retry_delay = max_retry_delay
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.requests_per_minute = requests_per_minute
        
        # Track request timing for pacing
        self.last_request_time = 0.0
        self.request_count = 0
        self.window_start = time.time()
    
    def with_retry(self, func: Callable) -> Callable:
        """
        Decorator to add retry logic with exponential backoff to a function.
        
        Usage:
            @rate_limiter.with_retry
            def my_api_call():
                return api.extract(...)
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            return self._execute_with_retry(func, *args, **kwargs)
        return wrapper
    
    def _execute_with_retry(self, func: Callable, *args, **kwargs) -> Any:
        """Execute function with retry logic."""
        # Apply proactive rate limiting if configured
        if self.requests_per_minute:
            self._pace_request()
        
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                result = func(*args, **kwargs)
                
                # Success! Reset window if we were tracking failures
                if attempt > 0:
                    logger.info(f"✓ Request succeeded after {attempt} retries")
                
                return result
                
            except Exception as e:
                last_exception = e
                error_msg = str(e).lower()
                
                # Detect rate limit errors
                is_rate_limit = any(
                    phrase in error_msg 
                    for phrase in [
                        'rate limit', 'too many requests', '429', 
                        'quota exceeded', 'throttled', 'rate_limit_exceeded'
                    ]
                )
                
                # Detect retryable errors
                is_retryable = is_rate_limit or any(
                    phrase in error_msg
                    for phrase in [
                        'timeout', 'connection', 'temporary', 'service unavailable',
                        '500', '502', '503', '504'
                    ]
                )
                
                if not is_retryable:
                    # Don't retry non-retryable errors
                    logger.error(f"Non-retryable error: {e}")
                    raise
                
                # Calculate retry delay
                if is_rate_limit:
                    # For rate limits, use longer delays
                    delay = self._calculate_rate_limit_delay(attempt, error_msg)
                    logger.warning(
                        f"⚠️  Rate limit hit (attempt {attempt + 1}/{self.max_retries}). "
                        f"Waiting {delay:.1f}s before retry..."
                    )
                else:
                    # For other errors, use standard backoff
                    delay = self._calculate_backoff_delay(attempt)
                    logger.warning(
                        f"⚠️  Retryable error (attempt {attempt + 1}/{self.max_retries}): {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                else:
                    logger.error(f"❌ Max retries ({self.max_retries}) reached")
        
        # All retries exhausted
        raise last_exception
    
    def _calculate_backoff_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay with jitter."""
        import random
        
        # Exponential: 2, 4, 8, 16, ... seconds
        delay = self.initial_retry_delay * (self.backoff_factor ** attempt)
        
        # Cap at max delay
        delay = min(delay, self.max_retry_delay)
        
        # Add jitter (±20% randomness) to avoid thundering herd
        jitter = delay * 0.2 * (2 * random.random() - 1)
        
        return delay + jitter
    
    def _calculate_rate_limit_delay(self, attempt: int, error_msg: str) -> float:
        """
        Calculate delay for rate limit errors.
        
        Tries to extract retry-after from error message, otherwise uses exponential backoff.
        """
        import re
        
        # Try to extract retry-after time from error message
        retry_after_match = re.search(r'retry[- ]after:?\s*(\d+)', error_msg, re.IGNORECASE)
        if retry_after_match:
            retry_after = int(retry_after_match.group(1))
            logger.info(f"Using retry-after from API: {retry_after}s")
            return retry_after
        
        # Try to extract seconds from message like "try again in 60 seconds"
        seconds_match = re.search(r'(?:in|after)\s+(\d+)\s*seconds?', error_msg, re.IGNORECASE)
        if seconds_match:
            seconds = int(seconds_match.group(1))
            logger.info(f"Using wait time from error message: {seconds}s")
            return seconds
        
        # Fallback to aggressive exponential backoff for rate limits
        # Start at 10s and double: 10, 20, 40, 80 seconds
        delay = 10.0 * (2 ** attempt)
        delay = min(delay, self.max_retry_delay)
        
        return delay
    
    def _pace_request(self):
        """
        Proactively pace requests to stay under rate limit.
        
        If requests_per_minute is set, ensures we don't exceed that rate.
        """
        if not self.requests_per_minute:
            return
        
        current_time = time.time()
        
        # Reset window if more than 60 seconds have passed
        if current_time - self.window_start >= 60:
            self.window_start = current_time
            self.request_count = 0
        
        # Check if we've hit the limit for this window
        if self.request_count >= self.requests_per_minute:
            # Wait until window resets
            wait_time = 60 - (current_time - self.window_start)
            if wait_time > 0:
                logger.info(f"⏱️  Rate limit pacing: waiting {wait_time:.1f}s for window reset")
                time.sleep(wait_time)
                self.window_start = time.time()
                self.request_count = 0
        
        # Add minimum delay between requests (avoid burst)
        min_delay = 60.0 / self.requests_per_minute
        time_since_last = current_time - self.last_request_time
        
        if time_since_last < min_delay:
            sleep_time = min_delay - time_since_last
            time.sleep(sleep_time)
        
        # Track this request
        self.last_request_time = time.time()
        self.request_count += 1


def create_rate_limiter(
    model: str = "gpt-4",
    conservative: bool = False
) -> RateLimitHandler:
    """
    Create a rate limiter with appropriate settings for the model.
    
    Args:
        model: Model name (e.g., "gpt-4", "gpt-3.5-turbo")
        conservative: Use conservative limits (lower throughput, safer)
        
    Returns:
        Configured RateLimitHandler
    """
    # Default conservative limits
    rpm = 20 if conservative else 40  # Requests per minute
    
    # Model-specific limits (can be adjusted based on your tier)
    if "gpt-4" in model.lower():
        # GPT-4 typically has lower limits
        rpm = 10 if conservative else 20
    elif "gpt-3.5" in model.lower():
        # GPT-3.5-turbo has higher limits
        rpm = 50 if conservative else 100
    
    logger.info(f"Configuring rate limiter: {rpm} requests/min (conservative={conservative})")
    
    return RateLimitHandler(
        initial_retry_delay=2.0,
        max_retry_delay=120.0,
        max_retries=5,
        backoff_factor=2.0,
        requests_per_minute=rpm,
    )
