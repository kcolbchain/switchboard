package conformance

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"
)

type vector struct {
	Name         string `json:"name"`
	FrameType    string `json:"frame_type"`
	Seq          uint64 `json:"seq"`
	Capabilities uint16 `json:"capabilities"`
	PayloadHex   string `json:"payload_hex"`
	WireHex      string `json:"wire_hex"`
}

var frameTypes = map[string]byte{
	"HELLO":     0x01,
	"HELLO_ACK": 0x02,
	"DATA":      0x03,
	"ACK":       0x04,
	"FIN":       0x05,
	"ERROR":     0x06,
}

func encodeVector(v vector) ([]byte, error) {
	payload, err := hex.DecodeString(v.PayloadHex)
	if err != nil {
		return nil, err
	}
	wire := make([]byte, 20+len(payload))
	copy(wire[0:4], []byte("ZAP!"))
	wire[4] = 0x01
	wire[5] = frameTypes[v.FrameType]
	binary.BigEndian.PutUint16(wire[6:8], v.Capabilities)
	binary.BigEndian.PutUint64(wire[8:16], v.Seq)
	binary.BigEndian.PutUint32(wire[16:20], uint32(len(payload)))
	copy(wire[20:], payload)
	return wire, nil
}

func TestZapWireV1Vectors(t *testing.T) {
	body, err := os.ReadFile("zap_wire_v1_vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	var vectors []vector
	if err := json.Unmarshal(body, &vectors); err != nil {
		t.Fatal(err)
	}
	for _, v := range vectors {
		t.Run(v.Name, func(t *testing.T) {
			wire, err := encodeVector(v)
			if err != nil {
				t.Fatal(err)
			}
			if got := hex.EncodeToString(wire); got != v.WireHex {
				t.Fatalf("wire mismatch\nwant %s\n got %s", v.WireHex, got)
			}
			if string(wire[0:4]) != "ZAP!" {
				t.Fatal("invalid magic")
			}
			if wire[4] != 0x01 {
				t.Fatalf("unexpected version %d", wire[4])
			}
			if binary.BigEndian.Uint64(wire[8:16]) != v.Seq {
				t.Fatal("sequence mismatch")
			}
		})
	}
}
