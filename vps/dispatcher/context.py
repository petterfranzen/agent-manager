"""What one invocation works with."""


class Context:
    """The parsed arguments (`a`), the board, the two model-side keys and the redacting log."""

    def __init__(self, a, board, keys, log):
        self.a, self.board, self.log = a, board, log
        self.hermes_key, self.orkey = keys
