"""
Tests for Switchboard billing surface.
"""

import pytest
from decimal import Decimal
from switchboard.billing import (
    BillingManager,
    BillingTier,
    TierConfig,
    TIER_CONFIGS,
)


class TestBillingManager:
    """Tests for BillingManager class."""
    
    def setup_method(self):
        self.billing = BillingManager()
    
    def test_get_tier_default(self):
        """Test that unknown customers get STANDARD tier."""
        assert self.billing.get_tier("unknown-customer") == BillingTier.STANDARD
    
    def test_get_tier_config_standard(self):
        """Test Standard tier config."""
        config = self.billing.get_tier_config(BillingTier.STANDARD)
        assert config.name == "Standard"
        assert config.included_tx_per_month == 1000
        assert config.overage_rate == Decimal("0.001")
        assert config.monthly_price == Decimal("0")
        assert config.annual_price is None
    
    def test_get_tier_config_pro(self):
        """Test Pro tier config."""
        config = self.billing.get_tier_config(BillingTier.PRO)
        assert config.name == "Pro"
        assert config.included_tx_per_month == 10000
        assert config.overage_rate == Decimal("0.0008")
        assert config.monthly_price == Decimal("99")
        assert config.annual_price == Decimal("1069.20")
    
    def test_record_tx(self):
        """Test recording a transaction."""
        self.billing.record_tx("customer-1", "0xabc123")
        assert self.billing.get_tx_count("customer-1") == 1
        
        self.billing.record_tx("customer-1", "0xdef456")
        assert self.billing.get_tx_count("customer-1") == 2
    
    def test_calculate_overage_none(self):
        """Test overage calculation when under limit."""
        self.billing.record_tx("customer-1", "0xabc123")
        overage = self.billing.calculate_overage("customer-1")
        assert overage == Decimal("0")
    
    def test_calculate_overage_standard(self):
        """Test overage calculation for Standard tier."""
        # Add 1001 transactions (over the 1000 limit)
        for i in range(1001):
            self.billing.record_tx("customer-1", f"0x{i:06x}")
        
        overage = self.billing.calculate_overage("customer-1")
        # 1 overage tx * $0.001 = $0.001
        assert overage == Decimal("0.001")
    
    def test_generate_invoice_standard(self):
        """Test invoice generation for Standard tier."""
        for i in range(100):
            self.billing.record_tx("customer-1", f"0x{i:06x}")
        
        invoice = self.billing.generate_invoice("customer-1")
        assert invoice["customer_id"] == "customer-1"
        assert invoice["tier"] == "standard"
        assert invoice["tx_count"] == 100
        assert invoice["base_price"] == "0"
        assert invoice["total"] == "0"
    
    def test_generate_invoice_with_overage(self):
        """Test invoice generation with overage."""
        for i in range(1500):
            self.billing.record_tx("customer-1", f"0x{i:06x}")
        
        invoice = self.billing.generate_invoice("customer-1")
        assert invoice["tx_count"] == 1500
        assert invoice["overage_tx"] == 500
        # 500 * $0.001 = $0.50
        assert invoice["overage_amount"] == "0.50"
        assert invoice["total"] == "0.50"
