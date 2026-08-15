"""HTTP surface.

Thin by design: every route parses input, calls into `gemp.services` or the domain
layer, and serialises the result. No physics, no solver configuration and no SQL
lives here, so the optimizer stays runnable from the command line with no web server
in the picture.
"""
