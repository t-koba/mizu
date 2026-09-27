"""Errors with stable, shell-friendly exit codes."""


class MizuError(Exception):
    code = 1


class ConfigError(MizuError):
    code = 78


class Busy(MizuError):
    code = 75


class Denied(MizuError):
    code = 77


class LimitExceeded(MizuError):
    code = 69


class Cancelled(MizuError):
    code = 130


class ProtocolError(MizuError):
    code = 76
