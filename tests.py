import unittest
import pgc_interface

class TestPoly(unittest.TestCase):

    def test_poly(self):
        polyglot = pgc_interface.Interface('Test')
        self.assertIsInstance(polyglot, pgc_interface.Interface)


if __name__ == "__main__":
    unittest.main()
