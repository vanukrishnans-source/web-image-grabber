import unittest

from wig import extract, net


class ExtractTests(unittest.TestCase):
    def test_srcset_largest_with_commas_in_url(self):
        r = extract.largest_of_srcset("https://c/w_1,h_2/a.jpg 100w, https://c/w_3,h_4/a.jpg 300w")
        self.assertEqual(r[0], "https://c/w_3,h_4/a.jpg")

    def test_density_srcset(self):
        self.assertEqual(extract.largest_of_srcset("a.jpg, b.jpg 2x, c.jpg 1.5x")[0], "b.jpg")

    def test_page(self):
        html = '''<title>T</title><meta property="og:image" content="/og.jpg"><img src="a.jpg" data-src="b.jpg">
        <div style="background:url('c.png')"></div><video poster=p.jpg></video>'''
        res, _, _ = extract.parse_html(html, "http://x.test/d/")
        urls = [c.url for c in extract.dedupe(res.candidates)]
        for u in ("http://x.test/og.jpg", "http://x.test/d/a.jpg", "http://x.test/d/b.jpg", "http://x.test/d/c.png", "http://x.test/d/p.jpg"):
            self.assertIn(u, urls)

    def test_wikimedia_original(self):
        u = "https://upload.wikimedia.org/wikipedia/commons/thumb/1/15/A_b.jpg/330px-A_b.jpg"
        self.assertEqual(extract.wikimedia_original(u), "https://upload.wikimedia.org/wikipedia/commons/1/15/A_b.jpg")

    def test_filenames(self):
        self.assertEqual(net.finalize_filename("noext", "png"), "noext.png")
        self.assertEqual(net.finalize_filename("a.php", "jpg"), "a.jpg")
        self.assertEqual(net.finalize_filename("a.jpeg", "jpg"), "a.jpeg")


if __name__ == "__main__":
    unittest.main()
