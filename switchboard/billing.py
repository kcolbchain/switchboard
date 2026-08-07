"""
Switchboard Hosted Billing Surface

Implements the Standard/Pro tier billing system for Hosted Switchboard.
Tracks per-tx metering and provides tier information for customer dashboard.

Tiers:
- Standard: 1000 tx/month included, $0.001/tx overage
- Pro: 10000 tx/month included, $0.0008/tx overage (10% discount for annual)

Usage:
    from switchboard.billing import BillingManager
    
    billing = BillingManager(stripe_key="sk_...")
    tier = billing.get_tier("customer-123")
    billing.record_tx("customer-123", tx_hash="0x...")
    invoice = billing.generate_invoice("customer-123")
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Optional


class BillingTier(Enum):
    """Supported billing tiers."""
    STANDARD = "standard"
    PRO = "pro"


@dataclass
class TierConfig:
    """Configuration for a billing tier."""
    name: str
    included_tx_per_month: int
    overage_rate: Decimal  # per tx
    monthly_price: Decimal
    annual_price: Optional[Decimal]  # None for Standard
    annual_discount: Decimal = Decimal("0.10")


TIER_CONFIGS = {
    BillingTier.STANDARD: TierConfig(
        name="Standard",
        included_tx_per_month=1000,
        overage_rate=Decimal("0.001"),
        monthly_price=Decimal("0"),  # Free tier
        annual_price=None,
    ),
    BillingTier.PRO: TierConfig(
        name="Pro",
        included_tx_per_month=10000,
        overage_rate=Decimal("0.0008"),
        monthly_price=Decimal("99"),
        annual_price=Decimal("1069.20"),  # $99 * 12 * 0.9
        annual_discount=Decimal("0.10"),
    ),
}


@dataclass
class CustomerBilling:
    """Billing state for a customer."""
    customer_id: str
    tier: BillingTier
    tx_count_current_month: int = 0
    billing_start: Optional[datetime] = None
    is_annual: bool = False


@dataclass
class TxRecord:
    """Record of a single transaction for metering."""
    tx_hash: str
    timestamp: datetime
    amount_wei: int
    customer_id: str


class BillingManager:
    """Manages billing for Hosted Switchboard customers."""
    
    def __init__(self, stripe_key: Optional[str] = None):
        self.stripe_key = stripe_key
        self.customers: dict[str, CustomerBilling] = {}
        self.tx_records: list[TxRecord] = []
    
    def get_tier(self, customer_id: str) -> BillingTier:
        """Get the billing tier for a customer."""
        if customer_id not in self.customers:
            return BillingTier.STANDARD
        return self.customers[customer_id].tier
    
    def get_tier_config(self, tier: BillingTier) -> TierConfig:
        """Get configuration for a billing tier."""
        return TIER_CONFIGS[tier]
    
    def record_tx(self, customer_id: str, tx_hash: str, amount_wei: int = 0) -> None:
        """Record a transaction for metering."""
        record = TxRecord(
            tx_hash=tx_hash,
            timestamp=datetime.utcnow(),
            amount_wei=amount_wei,
            customer_id=customer_id,
        )
        self.tx_records.append(record)
        
        # Update customer tx count
        if customer_id in self.customers:
            self.customers[customer_id].tx_count_current_month += 1
    
    def get_tx_count(self, customer_id: str) -> int:
        """Get current month's tx count for a customer."""
        if customer_id not in self.customers:
            return 0
        return self.customers[customer_id].tx_count_current_month
    
    def calculate_overage(self, customer_id: str) -> Decimal:
        """Calculate overage charges for a customer."""
        tier = self.get_tier(customer_id)
        config = self.get_tier_config(tier)
        tx_count = self.get_tx_count(customer_id)
        
        if tx_count <= config.included_tx_per_month:
            return Decimal("0")
        
        overage_tx = tx_count - config.included_tx_per_month
        return Decimal(str(overage_tx)) * config.overage_rate
    
    def generate_invoice(self, customer_id: str) -> dict:
        """Generate a monthly invoice for a customer."""
        tier = self.get_tier(customer_id)
        config = self.get_tier_config(tier)
        tx_count = self.get_tx_count(customer_id)
        overage = self.calculate_overage(customer_id)
        
        base_price = config.monthly_price
        if self.customers.get(customer_id, CustomerBilling(customer_id=customer_id, tier=tier)).is_annual:
            base_price = config.annual_price or config.monthly_price
        
        total = base_price + overage
        
        return {
            "customer_id": customer_id,
            "tier": tier.value,
            "billing_period": "monthly",
            "base_price": str(base_price),
            "tx_count": tx_count,
            "included_tx": config.included_tx_per_month,
            "overage_tx": max(0, tx_count - config.included_tx_per_month),
            "overage_rate": str(config.overage_rate),
            "overage_amount": str(overage),
            "total": str(total),
            "currency": "USD",
        }
