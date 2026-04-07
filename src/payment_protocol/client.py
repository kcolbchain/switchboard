import os
import time
from typing import Optional, Dict, Any, Union

from web3 import Web3
from web3.types import TxReceipt, HexBytes
from eth_account import Account
from eth_utils import to_checksum_address, keccak

from src.payment_protocol.models import PaymentRequest

# Placeholder ABI for EscrowProtocol (in a real project, this would be loaded from artifacts)
ESCROW_PROTOCOL_ABI = [
    {
        "inputs": [
            {"internalType": "bytes32", "name": "_escrowId", "type": "bytes32"},
            {"internalType": "address payable", "name": "_receiver", "type": "address"},
            {"internalType": "address", "name": "_token", "type": "address"},
            {"internalType": "uint256", "name": "_amount", "type": "uint256"},
            {"internalType": "uint256", "name": "_deadline", "type": "uint256"}
        ],
        "name": "createEscrow",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "_escrowId", "type": "bytes32"}
        ],
        "name": "releaseFunds",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "_escrowId", "type": "bytes32"}
        ],
        "name": "refundFunds",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "_escrowId", "type": "bytes32"}
        ],
        "name": "getEscrow",
        "outputs": [
            {"internalType": "bytes32", "name": "id", "type": "bytes32"},
            {"internalType": "address", "name": "sender", "type": "address"},
            {"internalType": "address", "name": "receiver", "type": "address"},
            {"internalType": "address", "name": "token", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
            {"internalType": "uint8", "name": "status", "type": "uint8"} # Enum `EscrowProtocol.EscrowStatus`
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

# Minimal ERC-20 ABI for approval and allowance checking
ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}, {"name": "_spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function",
    },
]

class EscrowClient:
    """
    A Python client for interacting with the EscrowProtocol smart contract.
    Allows agents to create, release, and refund escrowed payments.
    """
    def __init__(self, rpc_url: str, private_key: str, contract_address: str):
        """
        Initializes the EscrowClient.
        Args:
            rpc_url: The URL of the Ethereum/EVM-compatible blockchain RPC endpoint.
            private_key: The private key of the agent's wallet used to sign transactions.
            contract_address: The deployed address of the EscrowProtocol smart contract.
        """
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not self.w3.is_connected():
            raise ConnectionError(f"Failed to connect to web3 provider at {rpc_url}")

        self.account = Account.from_key(private_key)
        self.contract_address = to_checksum_address(contract_address)
        self.escrow_contract = self.w3.eth.contract(address=self.contract_address, abi=ESCROW_PROTOCOL_ABI)

        print(f"Initialized EscrowClient for sender: {self.account.address}")
        print(f"Connected to network chain_id: {self.w3.eth.chain_id}")

    def _send_transaction(self, tx_func, value: int = 0, gas_limit: Optional[int] = None) -> TxReceipt:
        """
        Helper method to build, sign, and send a transaction to the blockchain.
        Args:
            tx_func: The contract function call object (e.g., `contract.functions.myFunction(...)`).
            value: The amount of native currency (in wei) to send with the transaction.
            gas_limit: Optional, custom gas limit. If None, uses a default.
        Returns:
            The transaction receipt.
        Raises:
            Exception: If the transaction fails.
        """
        nonce = self.w3.eth.get_transaction_count(self.account.address)
        gas_price = self.w3.eth.gas_price

        # Estimate gas or use a default if estimate fails or is not desired for simplicity
        if gas_limit is None:
            try:
                gas_limit = tx_func.estimate_gas({'from': self.account.address, 'value': value})
                # Add a buffer to the estimated gas
                gas_limit = int(gas_limit * 1.2)
            except Exception as e:
                print(f"Warning: Could not estimate gas, using default (300,000). Error: {e}")
                gas_limit = 300_000 # Fallback default

        transaction = tx_func.build_transaction({
            'from': self.account.address,
            'nonce': nonce,
            'gasPrice': gas_price,
            'gas': gas_limit,
            'value': value,
        })

        signed_txn = self.w3.eth.account.sign_transaction(transaction, private_key=self.account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed_txn.rawTransaction)
        print(f"Transaction sent: {tx_hash.hex()}")
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)

        if receipt.status != 1:
            raise Exception(f"Transaction failed: {receipt}")
        print(f"Transaction successful: Block {receipt.blockNumber}, Gas Used: {receipt.gasUsed}")
        return receipt

    def generate_escrow_id(self, request: PaymentRequest) -> HexBytes:
        """
        Generates a deterministic (but unique for each attempt) escrow ID using keccak256 hash.
        Includes a random component to reduce collision risk if other parameters are identical.
        Args:
            request: The PaymentRequest object containing escrow details.
        Returns:
            A HexBytes object representing the 32-byte escrow ID.
        """
        data_string = f"{request.sender_address}-{request.receiver_address}-{request.amount}-{request.token_address}" \
                      f"-{request.deadline}-{request.reference_id}-{self.w3.eth.chain_id}-{os.urandom(8).hex()}"
        return HexBytes(keccak(text=data_string))

    def approve_erc20(self, token_address: str, amount: int) -> TxReceipt:
        """
        Approves the EscrowProtocol contract to spend a specified amount of an ERC-20 token
        on behalf of the current sender. This must be called before `create_escrow` for ERC-20 payments.
        Args:
            token_address: The contract address of the ERC-20 token.
            amount: The amount of tokens (in smallest unit) to approve.
        Returns:
            The transaction receipt of the approval.
        """
        token_contract = self.w3.eth.contract(address=to_checksum_address(token_address), abi=ERC20_ABI)
        tx_func = token_contract.functions.approve(self.contract_address, amount)
        print(f"Approving {amount} of ERC20 token {token_address} for escrow contract {self.contract_address}...")
        return self._send_transaction(tx_func)

    def get_erc20_allowance(self, token_address: str, owner: str, spender: str) -> int:
        """
        Checks the current ERC-20 allowance granted by an owner to a spender.
        Args:
            token_address: The contract address of the ERC-20 token.
            owner: The address that granted the allowance.
            spender: The address allowed to spend.
        Returns:
            The allowance amount (in smallest unit).
        """
        token_contract = self.w3.eth.contract(address=to_checksum_address(token_address), abi=ERC20_ABI)
        return token_contract.functions.allowance(owner, spender).call()

    def create_escrow(self, request: PaymentRequest) -> TxReceipt:
        """
        Creates an escrow on-chain by calling the `createEscrow` function of the smart contract.
        Deposits the specified funds into the escrow.
        Args:
            request: A PaymentRequest object containing all details for the escrow.
        Returns:
            The transaction receipt of the escrow creation.
        """
        if not request.escrow_id:
            request.escrow_id = self.generate_escrow_id(request)
            print(f"Generated new escrow_id: {request.escrow_id.hex()}")

        receiver_address_checksum = to_checksum_address(request.receiver_address)
        token_address_checksum = to_checksum_address(request.token_address)
        native_token_address = Web3.to_checksum_address('0x0000000000000000000000000000000000000000')

        value = 0
        if token_address_checksum == native_token_address:
            value = request.amount
            print(f"Creating ETH escrow {request.escrow_id.hex()} for {self.w3.from_wei(request.amount, 'ether')} ETH to {receiver_address_checksum}")
        else:
            print(f"Creating ERC20 escrow {request.escrow_id.hex()} for {request.amount} of token {token_address_checksum} to {receiver_address_checksum}")

        tx_func = self.escrow_contract.functions.createEscrow(
            request.escrow_id,
            receiver_address_checksum,
            token_address_checksum,
            request.amount,
            request.deadline
        )
        return self._send_transaction(tx_func, value=value)

    def release_funds(self, escrow_id: HexBytes) -> TxReceipt:
        """
        Releases funds from an escrow to the receiver.
        Only the sender (the client's connected account) can perform this action.
        Args:
            escrow_id: The unique identifier of the escrow to release.
        Returns:
            The transaction receipt of the fund release.
        """
        print(f"Releasing funds for escrow {escrow_id.hex()}...")
        tx_func = self.escrow_contract.functions.releaseFunds(escrow_id)
        return self._send_transaction(tx_func)

    def refund_funds(self, escrow_id: HexBytes) -> TxReceipt:
        """
        Refunds funds from an escrow back to the sender.
        Only the sender (the client's connected account) can perform this action,
        and only if the escrow's deadline has passed and funds are still deposited.
        Args:
            escrow_id: The unique identifier of the escrow to refund.
        Returns:
            The transaction receipt of the refund.
        """
        print(f"Refunding funds for escrow {escrow_id.hex()}...")
        tx_func = self.escrow_contract.functions.refundFunds(escrow_id)
        return self._send_transaction(tx_func)

    def get_escrow_status(self, escrow_id: HexBytes) -> Dict[str, Any]:
        """
        Retrieves the current status and detailed information of an escrow.
        Args:
            escrow_id: The unique identifier of the escrow.
        Returns:
            A dictionary containing the escrow's details.
        """
        print(f"Getting status for escrow {escrow_id.hex()}...")
        try:
            escrow_details = self.escrow_contract.functions.getEscrow(escrow_id).call()
            # Map the EscrowStatus enum (uint8) to a readable string
            status_map = {0: "Deposited", 1: "Released", 2: "Refunded"}
            return {
                "id": escrow_details[0].hex(),
                "sender": escrow_details[1],
                "receiver": escrow_details[2],
                "token": escrow_details[3],
                "amount": escrow_details[4],
                "deadline": escrow_details[5],
                "status": status_map.get(escrow_details[6], f"Unknown({escrow_details[6]})")
            }
        except Exception as e:
            print(f"Error getting escrow status for {escrow_id.hex()}: {e}")
            return {"error": str(e), "id": escrow_id.hex()}

