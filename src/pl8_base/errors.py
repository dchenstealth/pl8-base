class DDBError(Exception):
    """Base class for DDB Issues"""
    pass


class DDBInternalError(DDBError):
    """Raised for general service errors"""
    pass


class DDBCorruptedError(DDBInternalError):
    """Raised if data read from database is corrupted"""
    pass


class DDBMissingError(DDBError):
    """Raised when a requested item does not exist"""
    pass


class DDBExistsError(DDBError):
    """Raised when an item already exists"""
    pass


class DDBArgsError(DDBError):
    """Raised on invalid arguments passed in"""
    pass


class DDBIdCollisionError(DDBError):
    """Raised on ID collision after too many retries"""
    pass


class DDBTransactionConflictError(DDBError):
    """Raised on transaction conflict"""
    pass
