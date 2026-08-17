-- TSYC database security hardening
-- Records the production hardening changes already applied manually.
-- Do not re-run on production unless the current database state has been verified.

ALTER TABLE public.candidate_reference_sources
ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.internal_products
ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.product_contents
ENABLE ROW LEVEL SECURITY;

ALTER TABLE public.woocommerce_product_syncs
ENABLE ROW LEVEL SECURITY;

ALTER FUNCTION public.set_updated_at()
SET search_path = pg_catalog, pg_temp;