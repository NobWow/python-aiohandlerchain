import asyncio
# from collections import defaultdict
from contextlib import asynccontextmanager
from typing import MutableSequence, Coroutine, Callable, Any, Optional, AsyncGenerator, Tuple, Mapping


class AIOHandlerChain:
    """
    An implementation of event, but more advanced than regular asyncio.Event.
    Before emitting an underlying asyncio.Event, handler chain is executed,
    and if just one handler returns False, other handlers won't be executed
    If one handler returns True, all handlers will be executed regardless if
    next handlers returns False.
    Also possible to wait on the event before the handlers called, or after
    the handlers. There are two relying asyncio.Condition that are triggered
    during emitting.
    This class is callable, producing a coroutine for emitting an event
    """
    def __init__(self, *, event: Optional[asyncio.Event] = None, lock: Optional[asyncio.Lock] = None, cancellable=True):
        if not event:
            event = asyncio.Event()
        if not lock:
            lock = asyncio.Lock()
        self._handlers: MutableSequence[Callable[[type(self), ...], Coroutine[Any, Any, Any]]] = []
        self._lock = lock
        self._evt = event
        self._cancellable = cancellable
        self._before = asyncio.Condition(lock)
        self._after = asyncio.Condition(lock)
        self._ctxargs = []
        self._ctxkwargs = {}
        self._ctxres: Optional[bool] = None
        self._emitlock = asyncio.Lock()

    def isCancellable(self) -> bool:
        """
        Return True or False whether or not this handler chain is cancellable.
        This is determined at the object's creation as cancellable keyword-only argument.
        """
        return self._cancellable

    def _ctxhandle(self, res: Optional[bool] = None) -> (bool, list, dict):
        """
        This method is returned by the asynchronous context manager.
        Returns availability state and arguments, keyword arguments of the event.
        """
        _res = True
        if self._ctxres is None and res is not None and self._cancellable:
            _res = self._ctxres = res
            self.debug_print('_ctxhandle: self._ctxres updated to %s' % self._ctxres)
        elif self._ctxres is False:
            _res = False
        return _res, self._ctxargs, self._ctxkwargs

    def debug_print(self, msg: str):
        """For developers. Do whatever to display a debug print. Defaults to nothing"""
        pass

    def add_handler(self, afunc: Callable[[Any], Optional[Coroutine[Any, Any, Any]]]) -> bool:
        """
        Add callable or a coroutine function to this handler chain.
        Return True if successful, False otherwise
        """
        if afunc not in self._handlers:
            self._handlers.insert(0, afunc)
            return True
        return False

    def remove_handler(self, afunc: Callable) -> bool:
        """
        Remove coroutine function from this handler chain.
        Return True if successful, False otherwise
        """
        if afunc in self._handlers:
            self._handlers.remove(afunc)
            return True
        return False

    async def wait_for_successful(self):
        """
        Blocks until this event emits successfully.
        Invokes wait() method on underlying event.
        """
        await self._evt.wait()

    @asynccontextmanager
    async def wait_and_handle(self, *, kwarg_predicate: Optional[Mapping[Any, Any]] = None, predicate: Optional[Callable[[list, dict], bool]] = None, before=False) -> AsyncGenerator[Any, Callable[[Optional[bool]], Tuple[bool, list, dict]]]:
        """
        Blocks until this event emits and returns a context manager with handle after
        the handler chain.
        The handle of returned context manager is callable: it returns a tuple with
        3 values: bool, list, dict
        First is True if this event is not cancelled, False otherwise
        The handle can set the state of the execution: pass False to cancel this event,
        and True to force this event to be dispatched successfully.
        Others are positioned arguments and keyword arguments passed by emit().
        If before=True, waits until this event emits before executing event handlers,
        in this case when False is passed into handle, an entire handler chain won't
        be executed and this event will be cancelled.
        If predicate is specified, waits until specified predicate becomes true
        """
        _cond = self._before if before else self._after
        self.debug_print("wait_and_handle: before try")
        if _cond.locked():
            self.debug_print("wait_and_handle: condition is locked, passing...")
            async with _cond:
                pass
        try:
            await _cond.acquire()
            self.debug_print("wait_and_handle: lock acquired, waiting...")
            _predicate_available = isinstance(predicate, Callable)
            _kwargp_available = isinstance(kwarg_predicate, Mapping)
            if not _predicate_available:
                def predicate(*x):
                    return None
            if _predicate_available or _kwargp_available:
                def _predicate() -> bool:
                    _kwargp_res = False
                    if _kwargp_available:
                        # return True if whole kwarg predicate matches
                        _kwargp_res = kwarg_predicate.items() <= self._ctxkwargs.items()
                    return predicate(self._ctxargs, self._ctxkwargs) or _kwargp_res
                await _cond.wait_for(_predicate)
            else:
                await _cond.wait()
            self.debug_print("wait_and_handle: wait complete")
            yield self._ctxhandle
        finally:
            _cond.release()

    @asynccontextmanager
    async def emit_and_handle(self, *args, before=False, kwargs: Optional[dict] = None) -> AsyncGenerator[Any, Callable[[Optional[bool]], Tuple[bool, list, dict]]]:
        """
        Unlike wait_and_handle(), it doesn't block until an event occurs. Instead, it emits
        an event and handles it in some way when the handlers are not necessarily required
        to be triggered. Using this way of event emission gives an opportunity to determine
        whether or not this event emission is successful.
        Returns a handle which works in the same way as by wait_and_handle()
        """
        if kwargs is None:
            kwargs = {}
        self.debug_print("emit_and_handle: before try")
        if self._emitlock.locked():
            async with self._emitlock:
                pass
        self.debug_print("emit_and_handle: sleep 0")
        await asyncio.sleep(0)  # pass emit
        if before:
            self.debug_print("emit_and_handle: before true")
            async with self._emitlock:
                self.debug_print("emit_and_handle: emitlock acquired")
                async with self._lock:
                    self.debug_print("emit_and_handle (before): acquired lock, doing all the stuff before emitting")
                    yield self._ctxhandle
                self.debug_print("emit_and_handle (before): released lock, done stuff")
            self.debug_print("emit_and_handle: emitlock released")
            await self.emit(*args, **kwargs)
        else:
            self.debug_print("emit_and_handle: after true")
            async with self._after:
                asyncio.create_task(self.emit(*args, **kwargs))
                self.debug_print("emit_and_handle (after): lock acquired, waiting...")
                await self._after.wait()  # now emit should notify us, suspend emit() until context code completes
                await asyncio.sleep(0)  # pass emit
                yield self._ctxhandle
                self.debug_print("emit_and_handle (after): wait complete")

    async def __call__(self, *args, **kwargs) -> bool:
        """
        Shorthand for self.emit(*args, **kwargs)
        """
        return await self.emit(*args, **kwargs)

    async def emit(self, *args, **kwargs) -> bool:
        """
        Execute the handler chain and proceed with underlying event trigger
        in case of successful execution. Also triggers other tasks that are
        waiting for this event with self.wait_and_handle()
        Before acquiring the lock, it waits until this lock is completely unlocked.
        Note: this function will block until all the handlers are executed.
        """
        try:
            res = self._ctxres
            # prevent emit overlapping
            async with self._emitlock:
                self.debug_print("emit: checkout lock")
                self._ctxargs.extend(args)
                ctxargs = self._ctxargs
                self._ctxkwargs.update(kwargs)
                ctxkwargs = self._ctxkwargs
                async with self._before:
                    self.debug_print("emit: notifying _before")
                    self._before.notify_all()
                    await asyncio.sleep(0)  # pass waiters
                while self._before.locked():
                    self.debug_print("emit: locked, waiting...")
                    async with self._before:
                        pass
                    self.debug_print("emit: unlocked, continuing?")
                    await asyncio.sleep(0)  # pass another dispatcher
                self.debug_print("emit: updated context args")
                if self._ctxres is not False:
                    self.debug_print("emit: _ctxres is not False")
                    async with self._lock:
                        self.debug_print("emit: handler lock acquired")
                        for hndid in range(len(self._handlers)):
                            handler = self._handlers[hndid]
                            try:
                                if asyncio.iscoroutinefunction(handler):
                                    self._ctxres = _res = await handler(self, *ctxargs, **ctxkwargs)
                                else:
                                    self._ctxres = _res = handler(self, *ctxargs, **ctxkwargs)
                                if isinstance(self._ctxres, bool) and not res:
                                    res = _res
                                    if not _res:
                                        break
                            except Exception as exc:
                                await self.on_handler_error(hndid, exc, ctxargs, ctxkwargs)
                else:
                    self.debug_print("emit: _ctxres is False")
                    self._ctxargs.clear()
                    self._ctxkwargs.clear()
                async with self._after:
                    self.debug_print("emit: notifying _after")
                    self._after.notify_all()
                    await asyncio.sleep(0)  # pass waiters
                while self._after.locked():
                    self.debug_print("emit: after lock wait...")
                    async with self._after:
                        pass
                    await asyncio.sleep(0)  # pass another dispatcher
                self.debug_print("emit: end phase")
                if self._ctxres is not False:
                    self.debug_print("emit: checkout lock at the end")
                    async with self._lock:
                        pass
                    self.debug_print('emit: lock passed')
                if isinstance(self._ctxres, bool):
                    res = self._ctxres
                if res is not False:
                    self.debug_print('emit: execute success')
                    self._evt.set()
                    self._evt.clear()
                    await self.on_success(*ctxargs, **ctxkwargs)
                    return True
                else:
                    self.debug_print('emit: execute failure')
                    await self.on_failure(*ctxargs, **ctxkwargs)
                    return False
        except Exception as exc:
            await self.on_error(exc, args, kwargs)
        finally:
            self._ctxres = None
            self._ctxargs.clear()
            self._ctxkwargs.clear()

    async def on_success(self, *args, **kwargs):
        """
        Executed by emit() in case of successful execution
        """
        # override
        pass

    async def on_failure(self, *args, **kwargs):
        """
        Executed by emit() in case of cancellation
        """
        # override
        pass

    async def on_error(self, exc: Exception, *args, **kwargs):
        """
        Executed by emit() in case of an exception in it
        """
        # override
        pass

    async def on_handler_error(self, hndid: int, exc: Exception, *args, **kwargs):
        """
        Executed by emit() in case if one of the handlers fails to execute
        """
        # override
        pass
