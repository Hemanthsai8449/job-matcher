"""Business services for Job Matcher.

Routes should stay thin and call these modules for side effects and validation.  The
modules deliberately accept injectable clocks, transports, and mailers so their
security-sensitive behaviour can be tested without network access.
"""
