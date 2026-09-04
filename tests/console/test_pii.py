import unittest

from cryptography.exceptions import InvalidTag

from spark_console.pii import PiiCipher, mask_email, normalize_email


class PiiCipherTests(unittest.TestCase):
    def setUp(self):
        self.pii = PiiCipher(b"p" * 32)

    def test_email_is_normalized_encrypted_masked_and_aad_bound(self):
        self.assertEqual("2010039681@qq.com", normalize_email(" 2010039681@QQ.COM "))
        ciphertext, nonce = self.pii.encrypt_email(
            "2010039681@qq.com", aad=b"user:1"
        )
        self.assertNotIn(b"2010039681", ciphertext)
        self.assertEqual(
            "2010039681@qq.com",
            self.pii.decrypt_email(ciphertext, nonce, aad=b"user:1"),
        )
        with self.assertRaises(InvalidTag):
            self.pii.decrypt_email(ciphertext, nonce, aad=b"user:2")
        self.assertEqual("20******81@qq.com", mask_email("2010039681@qq.com"))

    def test_lookup_and_code_hashes_are_stable_but_domain_separated(self):
        self.assertEqual(
            self.pii.lookup_hash("User@Example.com"),
            self.pii.lookup_hash(" user@example.COM "),
        )
        digest = self.pii.code_hash("bind:user-1", "123456")
        self.assertTrue(self.pii.verify_code("bind:user-1", "123456", digest))
        self.assertFalse(self.pii.verify_code("register:user-1", "123456", digest))
        self.assertFalse(self.pii.verify_code("bind:user-1", "654321", digest))

    def test_rejects_invalid_email_and_key(self):
        for value in ("", "a@b", "a b@example.com", "@example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_email(value)
        with self.assertRaisesRegex(ValueError, "exactly 32 bytes"):
            PiiCipher(b"short")


if __name__ == "__main__":
    unittest.main()
