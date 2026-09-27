package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"time"

	"github.com/Origami74/gonuts-tollgate/cashu"
	"github.com/Origami74/gonuts-tollgate/cashu/nuts/nut04"
	"github.com/Origami74/gonuts-tollgate/wallet"
)

const defaultMintURL = "https://testnut-compat.mints.orangesync.tech"
const defaultAmount = 1013
const feeBuffer = 10

func main() {
	mintURL := defaultMintURL
	amount := uint64(defaultAmount)
	if len(os.Args) > 1 {
		mintURL = os.Args[1]
	}
	if len(os.Args) > 2 {
		_, err := fmt.Sscanf(os.Args[2], "%d", &amount)
		if err != nil {
			log.Fatalf("invalid amount: %v", err)
		}
	}

	tmpDir, err := os.MkdirTemp("", "mint-token-*")
	if err != nil {
		log.Fatalf("temp dir: %v", err)
	}
	defer os.RemoveAll(tmpDir)

	cfg := wallet.Config{
		WalletPath:     tmpDir,
		CurrentMintURL: mintURL,
	}
	w, err := wallet.LoadWallet(cfg)
	if err != nil {
		log.Fatalf("LoadWallet: %v", err)
	}

	mintAmount := amount + feeBuffer

	quote, err := w.RequestMint(mintAmount, mintURL)
	if err != nil {
		w.Shutdown()
		log.Fatalf("RequestMint(%d, %s): %v", mintAmount, mintURL, err)
	}

	deadline := time.Now().Add(30 * time.Second)
	for time.Now().Before(deadline) {
		state, err := w.MintQuoteState(quote.Quote)
		if err != nil {
			w.Shutdown()
			log.Fatalf("MintQuoteState: %v", err)
		}
		if state.State == nut04.Paid {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}

	minted, err := w.MintTokens(quote.Quote)
	if err != nil {
		w.Shutdown()
		log.Fatalf("MintTokens: %v", err)
	}

	balance := w.GetBalance()
	_ = minted

	sendResult, err := w.SendWithOptions(amount, mintURL, wallet.SendOptions{
		IncludeFees:            true,
		AllowOverpayment:       true,
		MaxOverpaymentPercent:  10000,
		MaxOverpaymentAbsolute: 500,
	})
	if err != nil {
		w.Shutdown()
		log.Fatalf("SendWithOptions: %v", err)
	}

	// Token format is configurable: V3 (default) works on every deployed
	// backend including field v0.5.0 (gonuts < v0.8.0 has no V4 short-keyset
	// resolution); V4 (cashuB CBOR) is what modern CDK-based wallets emit
	// and needs gonuts-tollgate >= v0.8.0 on the router.
	format := os.Getenv("MINT_TOKEN_FORMAT")
	if format == "" {
		format = "v3"
	}

	var tokenStr string
	switch format {
	case "v4":
		tokenV4, err := cashu.NewTokenV4(sendResult.Proofs, mintURL, cashu.Sat, true)
		if err != nil {
			w.Shutdown()
			log.Fatalf("NewTokenV4: %v", err)
		}
		tokenStr, err = tokenV4.Serialize()
		if err != nil {
			w.Shutdown()
			log.Fatalf("Serialize V4: %v", err)
		}
	case "v3":
		tokenV3, err := cashu.NewTokenV3(sendResult.Proofs, mintURL, cashu.Sat, true)
		if err != nil {
			w.Shutdown()
			log.Fatalf("NewTokenV3: %v", err)
		}
		tokenStr, err = tokenV3.Serialize()
		if err != nil {
			w.Shutdown()
			log.Fatalf("Serialize V3: %v", err)
		}
	default:
		w.Shutdown()
		log.Fatalf("unknown MINT_TOKEN_FORMAT %q (want v3 or v4)", format)
	}

	if err := w.Shutdown(); err != nil {
		log.Printf("warning: shutdown: %v", err)
	}

	result := map[string]interface{}{
		"token":       tokenStr,
		"amount":      amount,
		"balance":     balance,
		"overpayment": sendResult.Overpayment,
	}
	out, _ := json.Marshal(result)
	fmt.Println(string(out))
}
