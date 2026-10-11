# Synthetic manager import evidence

`synthetic.json` is entirely invented, including the sentinel entry ID
2147483647, player IDs, prices, events and picks. It contains no manager names
and was not captured from FPL. Tests derive transfers/chips/failures from this
synthetic baseline. No requests are made to any entry endpoint.

The envelope's `synthetic` marker is test metadata, not an FPL response field.
Each nested endpoint payload uses the consumed public response shape. The
transport status, fetch times and season are separate caller-supplied evidence.
