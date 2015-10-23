# std imports
import asyncio

# local
from . import server_base


class UnicodeMixin(server_base.BaseServer):
    """Provides unicode streams by negotiating encoding."""

    def __init__(self, encoding='utf8', encoding_error='replace',
                 force_binary=False, **kwargs):
        """
        :param str encoding: The default encoding preferred by the server
            if not otherwise negotiated.
        :param str encoding_error: Same meaning as :class:`codecs.Codec`.
        :param bool force_binary: When ``True``, the encoding specified is
            used for both directions without ``BINARY`` negotiation, rfc-856_.
        """
        # set default encoding, may be negotiated !
        self.default_encoding = encoding
        self._encoding_error = encoding_error
        self.force_binary = force_binary

        #: Future receives ``self`` as result after completion
        #: of encoding negotiation considered complete.
        self.waiter_encoding = asyncio.Future()

        self._tasks.append(self.waiter_encoding)
        super().__init__(self, **kwargs)

    def request_advanced_negotiation(self):
        """
        Request ``IAC WILL BINARY`` and ``IAC DO CHARSET``.

        Schedule :meth:`_check_encoding_negotiation` for continual
        callback to advance the completion of :attr:`waiter_encoding`
        """
        from .telopt import WILL, BINARY, DO, CHARSET
        super().request_advanced_opts()

        self.writer.iac(WILL, BINARY)
        self.writer.iac(DO, CHARSET)
        self._loop.call_soon(self.check_encoding_negotiation)

    def encoding(self, outgoing=None, incoming=None):
        """
        Return encoding for the given stream direction.

        :param bool outgoing: Whether the return value is suitable for
            encoding bytes for transmission to client end.
        :param bool incoming: Whether the return value is suitable for
            decoding bytes received from the client.
        :raises TypeError: when a direction argument, either ``outgoing``
            or ``incoming``, was not set ``True``.
        :returns: ``'US-ASCII'`` for the directions indicated, unless
            ``BINARY`` rfc-856_ has been negotiated for the direction
            indicated or :attr`force_binary` is set ``True``.
        :rtype: str

        Value resolution order (first-matching):

        - value set by :meth:`set_encoding`.
        - value of :meth:`get_extra_info` using key argument, ``LANG``.
        - value of :attr:`default_encoding`.
        - ``US-ASCII`` when binary transmission not allowed.
        """
        if not (outgoing or incoming):
            raise TypeError("encoding arguments 'outgoing' and 'incoming' "
                            "are required: toggle at least one.")

        # may we encode in the direction indicated?
        _outgoing_only = outgoing and not incoming
        _incoming_only = not outgoing and incoming
        _bidirectional = outgoing and incoming
        _may_encode = ((_outgoing_only and self.outbinary) or
                       (_incoming_only and self.inbinary) or
                       (_bidirectional and self.outbinary and self.inbinary))

        encoding = 'US-ASCII'
        if self.force_binary or _may_encode:
            encoding = self.default_encoding
            # TODO: how do we better parse LANG using std library?
            _lang = self.get_extra_info('LANG', None)
            if _lang and '.' in _lang:
                _, encoding = _lang.split('.', 1)
            encoding = self.get_extra_info('encoding', encoding)

        return encoding

    @property
    def inbinary(self):
        """Whether server status ``inbinary`` is toggled."""
        from .telopt import BINARY
        return self._stream.remote_option.enabled(BINARY)

    @property
    def outbinary(self):
        """Whether server status ``outbinary`` is toggled."""
        from .telopt import BINARY
        return self._stream.local_option.enabled(BINARY)

    def _check_encoding_negotiation(self):
        """Scheduled callback checks bi-directional encoding state success."""
        # Method schedules itself for continual callback until encoding
        # negotiation is considered final, setting 'waiter_encoding'
        # to value 'self' when complete.
        #
        # Negotiation is final when only 'outbinary' and 'inbinary' have
        # been answered in the affirmative.
        from .telopt import DO, BINARY
        if self._closing:
            return

        later = max(self.CONNECT_DEFERRED,
                    max(0, self.CONNECT_MAXWAIT - self.duration))

        # encoding negotiation is complete
        if self.outbinary and self.inbinary:
            encoding = self.encoding(outgoing=True, incoming=True)
            self.log.debug('encoding negotiated, {0!r}'.format(encoding))
            self.waiter_encoding.set_result(self)

        elif self.duration > self.CONNECT_MAXWAIT:
            # tintin++ for example, would not answer "DONT BINARY" after
            # having sent "WONT BINARY". These kinds of IAC interpreters may
            # be discovered by requesting (DO, ECHO): the client replies
            # (WILL, ECHO), which is preposterous!
            self.log.debug('encoding: negotiation failed.')
            self.waiter_encoding.set_result(self)

        # if (WILL, BINARY) requested by begin_negotiation() is answered in
        # the affirmative, then request (DO, BINARY) to ensure bi-directional
        # transfer of non-ascii characters.
        elif (
            self.outbinary and not self.inbinary and
            not DO + BINARY in self._stream.pending_option
        ):
            self.log.debug('encoding: outbinary=True, requesting inbinary.')
            self.writer.iac(DO, BINARY)
            self._loop.call_later(later, self.check_encoding_negotiation)

        else:
            self._loop.call_later(later, self.check_encoding_negotiation)


class TimeoutServerMixin(server_base.BaseServer):
    """BaseServer Mix-in closes peer after timeout."""

    def __init__(self, timeout=300, **kwargs):
        """
        :param int timeout: Forcefully disconnect client in callback
            method :meth:`on_timeout` after given seconds have elapsed
            without client input.
        """
        self._tasks.append(self.waiter_timeout)
        self._timer = asyncio.Future()
        self._extra['timeout'] = timeout

        self.waiter_timeout = asyncio.Future()
        self.waiter_timeout.add_done_callback(self.on_timeout)

    def data_received(self, data):
        """Derive and cause timer reset."""
        self.set_timeout()
        super().data_received(data)

    def set_timeout(self, duration=-1):
        """
        Restart or unset timeout for client.

        :param int duration: When specified as a positive integer,
            schedules Future :attr:`self.waiter_timeout` with attached
            instance callback :meth:`timeout`.  When ``-1``, the value
            of :meth:`get_extra_info` for keyword ``timeout`` is used.
            When non-True, :attr:`waiter_timeout` is cancelled.
        """
        self._timer.cancel()
        if duration == -1:
            duration = self.get_extra_info('timeout', 0)
        if duration:
            self._timer = self._loop.call_later(duration, self._raise_timeout)

    def on_timeout(self, result):
        """
        Callback received on session timeout.

        Default implementation closes transport.

        This method is added as callback to :class:`asyncio.Future`
        instance :attr:`waiter_timeout`, and can be disabled by calling
        :meth:`set_timeout` with :paramref:`~.set_timeout.duration`
        value of ``0``.
        """
        if not self._closing:
            # emit a simple farewell message to client before closing.
            self._transport.write(b'\r\nTimeout.\r\n')
            self._transport.close()

    def _raise_timeout(self):
        """Callback on :attr:`_timer` set by :meth:`set_timeout`."""
        if self._closing:
            return
        self.waiter_timeout.set_result(True)