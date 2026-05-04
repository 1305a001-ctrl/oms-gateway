"""oms-gateway — Phase 2 order-management gateway.

Pipeline:
    alphas:active (Redis Stream) → preflight (L0 caps + halt) → oms_intents (postgres)

In v0.1.0 the gateway is *paper-mode* and does NOT dispatch to broker
adapters. It records intents with status='queued' (accepted) or
status='rejected' (caps breached), publishing the gating decision back to
risk:alerts when a level-3 cap fires. Downstream dispatch (per-bucket
executors → trading-agent / poly-agent / forex-agent) is Phase 2.5.
"""

__version__ = "0.1.0"
