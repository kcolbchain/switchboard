import {
  IAgentPlugin,
  PluginRegistry,
  AgentAction,
  AgentProvider,
} from '@elizaos/core';

interface WalletConfig {
  chainId: number;
  label: string;
}

interface BudgetConfig {
  perHour: number;
  perDay: number;
}

interface X402Config {
  maxPaymentWei: number;
  allowedRecipients?: string[];
}

interface MPPConfig {
  apiKey: string;
  defaultLimitUsd: number;
}

interface SwitchboardConfig {
  parties: number;
  threshold: number;
  wallets: WalletConfig[];
  budget: BudgetConfig;
  x402: X402Config;
  mpp?: MPPConfig;
}

class SwitchboardPlugin implements IAgentPlugin {
  name = '@kcolbchain/eliza-switchboard';
  version = '0.1.0';
  description = 'Switchboard wallet, budget, x402, and MPP for ElizaOS agents';

  private config: SwitchboardConfig;
  private wallets: Map<string, { address: string; chainId: number }> = new Map();
  private budgetState: { hourly: number; daily: number } = { hourly: 0, daily: 0 };
  private x402Session: any = null;
  private mppSession: any = null;

  constructor(config: Partial<SwitchboardConfig> = {}) {
    this.config = {
      parties: 3,
      threshold: 2,
      wallets: [],
      budget: { perHour: 2_000_000, perDay: 20_000_000 },
      x402: { maxPaymentWei: 10 ** 16 },
      ...config,
    };
  }

  async onRegister(registry: PluginRegistry): Promise<void> {
    registry.registerAction(this.createWalletAction());
    registry.registerAction(this.signTransactionAction());
    registry.registerAction(this.checkBudgetAction());
    registry.registerAction(this.payX402Action());
    registry.registerAction(this.openMPPSessionAction());
    registry.registerProvider(this.walletProvider());
  }

  private createWalletAction(): AgentAction {
    return {
      name: 'createWallet',
      description: 'Create a new MPC wallet for a given chain',
      handler: async (params: any) => {
        const wallet: WalletConfig = {
          chainId: params.chainId || 1,
          label: params.label || `wallet-${this.wallets.size + 1}`,
        };
        const address = `0x${Array.from({ length: 40 }, () =>
          Math.floor(Math.random() * 16).toString(16)
        ).join('')}`;
        this.wallets.set(wallet.label, { address, chainId: wallet.chainId });
        return { success: true, wallet: { label: wallet.label, address, chainId: wallet.chainId } };
      },
    };
  }

  private signTransactionAction(): AgentAction {
    return {
      name: 'signTransaction',
      description: 'Sign a transaction using MPC',
      handler: async (params: any) => {
        const wallet = this.wallets.get(params.wallet || 'default');
        if (!wallet) throw new Error('Wallet not found');
        return { success: true, txHash: `0x${'0'.repeat(64)}`, from: wallet.address };
      },
    };
  }

  private checkBudgetAction(): AgentAction {
    return {
      name: 'checkBudget',
      description: 'Check remaining gas budget',
      handler: async () => {
        return {
          hourlyRemaining: this.config.budget.perHour - this.budgetState.hourly,
          dailyRemaining: this.config.budget.perDay - this.budgetState.daily,
        };
      },
    };
  }

  private payX402Action(): AgentAction {
    return {
      name: 'payX402',
      description: 'Respond to an x402 payment challenge',
      handler: async (params: any) => {
        if (!params.amountWei || !params.recipient) {
          throw new Error('amountWei and recipient required');
        }
        if (params.amountWei > this.config.x402.maxPaymentWei) {
          throw new Error('Payment exceeds x402 max');
        }
        this.budgetState.hourly += params.amountWei;
        this.budgetState.daily += params.amountWei;
        return {
          success: true,
          txHash: `0x${'a'.repeat(64)}`,
          payer: Array.from(this.wallets.values())[0]?.address || '',
        };
      },
    };
  }

  private openMPPSessionAction(): AgentAction {
    return {
      name: 'openMPPSession',
      description: 'Open an MPP streaming payment session',
      handler: async (params: any) => {
        const limitUsd = params.limitUsd || this.config.mpp?.defaultLimitUsd || 10;
        this.mppSession = { sessionId: `session-${Date.now()}`, limitUsd, spentUsd: 0 };
        return { success: true, session: this.mppSession };
      },
    };
  }

  private walletProvider(): AgentProvider {
    return {
      name: 'switchboard',
      description: 'Switchboard wallet and budget provider',
      get: async () => ({
        wallets: Array.from(this.wallets.values()),
        budget: this.config.budget,
      }),
    };
  }
}

export function createSwitchboardPlugin(config?: Partial<SwitchboardConfig>): SwitchboardPlugin {
  return new SwitchboardPlugin(config);
}

export default SwitchboardPlugin;
