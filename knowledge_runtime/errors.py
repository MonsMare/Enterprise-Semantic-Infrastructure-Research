class KnowledgeRuntimeError(Exception):
    code = "KR_ERROR"


class KRNotFound(KnowledgeRuntimeError):
    code = "NOT_FOUND"


class KRInvalidLocator(KnowledgeRuntimeError):
    code = "INVALID_LOCATOR"


class KRStaleLocator(KnowledgeRuntimeError):
    code = "STALE_LOCATOR"


class KRUnsupported(KnowledgeRuntimeError):
    code = "UNSUPPORTED"


class KRLimitExceeded(KnowledgeRuntimeError):
    code = "LIMIT_EXCEEDED"


class KRCursorInvalid(KnowledgeRuntimeError):
    code = "CURSOR_INVALID"


class KRQueryInvalid(KnowledgeRuntimeError):
    code = "QUERY_INVALID"


class KRProviderUnavailable(KnowledgeRuntimeError):
    code = "PROVIDER_UNAVAILABLE"
