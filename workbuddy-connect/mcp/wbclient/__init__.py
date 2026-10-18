"""wbclient — WorkBuddy client library for the workbuddy-connect ZCode plugin.

A Python port of the protocol used by
[corrinehu/dsh-workbuddy-connect](https://github.com/corrinehu/dsh-workbuddy-connect)
(MIT License, Copyright © 2026 Corrine Hu): desktop credential reuse
(including the 5.6+ at-rest encrypted form), token refresh, the model
catalog, credit balance and chat streaming over the WorkBuddy /
WorkBuddy AI client interfaces.
"""

from . import auth, catalog, doctor, upstream, variants  # noqa: F401
