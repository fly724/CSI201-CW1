"""quiznet - networked quiz tournament implementation (Python 3.12 standard library only).

Modules
-------
protocol      : length-prefixed JSON framing, envelope construction and validation
questionbank  : compulsory question-bank loading and validation
server        : selector-driven authoritative game server
client        : command-line client and event renderer
"""

__all__ = ["protocol", "questionbank", "server", "client"]
__version__ = "1.0.0"
