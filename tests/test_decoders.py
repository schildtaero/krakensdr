"""Protocol tests, including error rejection and sample-boundary provenance."""

import itertools
import unittest

import numpy as np

from krakendf.decoders import (
    _POCSAG_IDLE,
    _POCSAG_SYNC,
    _decode_bits,
    _make_word,
    _repair_word,
    _syndrome,
    decode_lora,
    decode_pocsag,
    encode_pocsag_bits,
    parse_bft_payload,
    synthesize_pocsag,
)


PAYLOAD = "DL7CR 49.712340 11.045670 312m 184205Z 07sat"


def fsk_from_bits(bits, fs=48000, noise_std=0.01):
    # Tests also exercise the bit parser independently of demodulation.
    rng = np.random.default_rng(16)
    level = np.repeat(2 * np.asarray(bits, dtype=float) - 1, int(fs / 1200))
    iq = np.exp(2j * np.pi * np.cumsum(4500 * level) / fs)
    return (iq + noise_std * (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq)))).astype(np.complex64)


class PayloadParserTests(unittest.TestCase):
    def test_full_position_and_suffix_callsign(self):
        got = parse_bft_payload(PAYLOAD.replace("DL7CR", "dl7cr/P"))
        self.assertEqual(got["callsign"], "DL7CR/P")
        self.assertAlmostEqual(got["latitude"], 49.712340)
        self.assertAlmostEqual(got["longitude"], 11.045670)
        self.assertEqual(got["position_utc"], "184205Z")
        self.assertEqual(got["satellites"], 7)
        self.assertTrue(got["position_valid"])

    def test_no_gps_and_lora_test_have_identity_without_coordinates(self):
        for suffix in ("NO GPS Test", "TESTmessageLoRaTST"):
            got = parse_bft_payload("DL7CR " + suffix)
            self.assertEqual(got["callsign"], "DL7CR")
            self.assertFalse(got["position_valid"])
            self.assertIsNone(got["latitude"])

    def test_invalid_positions_time_and_injected_controls_rejected(self):
        for text in (
            PAYLOAD.replace("49.712340", "91.000000"),
            PAYLOAD.replace("11.045670", "-181.000000"),
            PAYLOAD.replace("184205Z", "244205Z"),
            PAYLOAD.replace("184205Z", "186005Z"),
            PAYLOAD.replace("184205Z", "184260Z"),
            PAYLOAD.replace("DL7CR", "LONGCALL10"),
            PAYLOAD + "\n",
            PAYLOAD + " unexpected",
            "DL7CR 49.712340 11.045670",
            "=HYPERLINK 49.712340 11.045670 312m 184205Z 07sat",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_bft_payload(text))

    def test_zero_satellites_is_not_invented_into_a_fix_quality(self):
        # A valid envelope carries no HDOP/age; callers must assess quality.
        result = parse_bft_payload(PAYLOAD.replace("07sat", "00sat"))
        self.assertTrue(result["position_valid"])
        self.assertEqual(result["satellites"], 0)


class PocsagCodeTests(unittest.TestCase):
    def test_known_standard_sync_idle_vectors(self):
        self.assertEqual(_syndrome(0x7CD215D8), 0)
        self.assertEqual(_syndrome(0x7A89C197), 0)
        self.assertEqual(_make_word(0x7A89C197 >> 11), 0x7A89C197)

    def test_all_one_and_two_bit_errors_including_parity_correct(self):
        original = _POCSAG_IDLE
        for weight in (1, 2):
            for locations in itertools.combinations(range(32), weight):
                mask = sum(1 << x for x in locations)
                self.assertEqual(_repair_word(original ^ mask), (original, weight))

    def test_all_three_bit_errors_detected(self):
        original = _POCSAG_IDLE
        for locations in itertools.combinations(range(32), 3):
            mask = sum(1 << x for x in locations)
            self.assertIsNone(_repair_word(original ^ mask))

    def test_address_frame_index_and_message_batches(self):
        for ric in (1000000, 1000001, 1000007):
            got = _decode_bits(encode_pocsag_bits(PAYLOAD, ric=ric))
            matching = [x for x in got if x["payload"] == PAYLOAD]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["ric"], ric)
            self.assertEqual(matching[0]["function"], 3)
            self.assertEqual(matching[0]["bit_start"], 0)

    def test_uncorrectable_message_is_not_reassembled_from_missing_word(self):
        bits = encode_pocsag_bits(PAYLOAD)
        first_data = 576 + 32 + 32
        bits[first_data + np.array([3, 12, 20])] ^= 1
        self.assertFalse(_decode_bits(bits))

    def test_address_change_does_not_join_messages(self):
        # Construct a single batch with two addresses and independent payloads.
        # The two real callsigns must remain distinct despite sharing a RIC.
        def words_for(text):
            coded = encode_pocsag_bits(text)
            body = coded[608 : 608 + 16 * 32]
            result = []
            for i in range(0, len(body), 32):
                word = int("".join(map(str, body[i : i + 32])), 2)
                if word != _POCSAG_IDLE:
                    result.append(word)
            return result

        first, second = words_for("CALL1 HELLO"), words_for("CALL2 WORLD")
        words = first + second
        words += [_POCSAG_IDLE] * (16 - len(words))
        bits = np.array(
            [1 - (n & 1) for n in range(576)]
            + [(w >> b) & 1 for w in [_POCSAG_SYNC, *words] for b in range(31, -1, -1)],
            dtype=np.uint8,
        )
        got = _decode_bits(bits)
        self.assertEqual([x["payload"] for x in got], ["CALL1 HELLO", "CALL2 WORLD"])


class PocsagDemodTests(unittest.TestCase):
    def test_fsk_both_polarities_offset_and_noninteger_samples_per_bit(self):
        for rate, inverted, offset, ric in (
            (48000, False, 0, 1000000),
            (300000, True, 1200, 1000007),
            (25000, False, -1000, 1000000),
        ):
            with self.subTest(rate=rate, inverted=inverted):
                iq = synthesize_pocsag(PAYLOAD, rate, ric=ric, invert=inverted, frequency_offset_hz=offset)
                result = decode_pocsag(iq, rate)
                self.assertEqual(len(result), 1)
                packet = result[0]
                self.assertEqual(packet.payload, PAYLOAD)
                self.assertEqual(packet.quality["ric"], ric)
                self.assertTrue(packet.quality["parity_ok"])
                self.assertTrue(packet.quality["bch_ok"])
                self.assertTrue(packet.quality["bft_payload_valid"])
                self.assertLessEqual(abs(packet.sample_start - int(0.04 * rate)), rate / 1200)
                self.assertLessEqual(packet.sample_end, len(iq))
                self.assertGreater(packet.sample_end, packet.sample_start)

    def test_multiple_bursts_keep_their_input_sample_coordinates(self):
        rate = 48000
        one = synthesize_pocsag(PAYLOAD, rate)
        text2 = PAYLOAD.replace("DL7CR", "DL1ABC")
        two = synthesize_pocsag(text2, rate, seed=3)
        silence = np.zeros(int(rate * 0.15), dtype=np.complex64)
        iq = np.concatenate((one, silence, two))
        got = decode_pocsag(iq, rate)
        self.assertEqual([x.payload for x in got], [PAYLOAD, text2])
        self.assertLess(got[0].sample_end, got[1].sample_start)
        self.assertGreaterEqual(got[1].sample_start, len(one) + len(silence))

    def test_uncorrectable_codeword_never_marks_payload_valid(self):
        bits = encode_pocsag_bits(PAYLOAD)
        bits[640 + np.array([4, 16, 23])] ^= 1
        self.assertEqual(decode_pocsag(fsk_from_bits(bits), 48000), [])

    def test_silence_noise_short_and_invalid_input(self):
        self.assertEqual(decode_pocsag(np.zeros(48000, np.complex64), 48000), [])
        rng = np.random.default_rng(42)
        noise = (rng.normal(size=48000) + 1j * rng.normal(size=48000)).astype(np.complex64)
        self.assertEqual(decode_pocsag(noise, 48000), [])
        self.assertEqual(decode_pocsag(np.ones(20, np.complex64), 48000), [])
        with self.assertRaises(ValueError):
            decode_pocsag(np.ones((5, 100), np.complex64), 48000)
        with self.assertRaises(ValueError):
            decode_pocsag(np.array([complex(float("nan"), 0)]), 48000)


class LoRaWrapperTests(unittest.TestCase):
    def test_verified_position_packet_keeps_original_sample_indexes(self):
        from krakendf.lora_phy import synthesize_lora
        rate = 300000
        burst = synthesize_lora(PAYLOAD.encode("ascii"), sample_rate_hz=rate)
        padding = np.zeros(4131, np.complex64)
        iq = np.concatenate((padding, burst, padding))
        got = decode_lora(iq, rate)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].payload, PAYLOAD)
        self.assertEqual(parse_bft_payload(got[0].payload)["callsign"], "DL7CR")
        self.assertTrue(got[0].quality["header_ok"])
        self.assertTrue(got[0].quality["crc_ok"])
        self.assertTrue(got[0].quality["bft_payload_valid"])
        self.assertLess(abs(got[0].sample_start - len(padding)), rate / 125000 * 4)
        self.assertLessEqual(got[0].sample_end, len(iq))

    def test_bad_crc_never_becomes_a_position_observation(self):
        from krakendf.lora_phy import synthesize_lora
        burst = synthesize_lora(PAYLOAD.encode("ascii"), corrupt_crc=True)
        self.assertEqual(decode_lora(burst, 250000), [])


if __name__ == "__main__":
    unittest.main()
