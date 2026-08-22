-- All dates are relative to CURRENT_DATE so the demo never goes stale.

INSERT INTO customers (email, full_name, shipping_zip) VALUES
  ('sarah.chen@example.com',  'Sarah Chen',  '94110'),
  ('marcus.webb@example.com', 'Marcus Webb', '02139'),
  ('priya.raman@example.com', 'Priya Raman', '60614');

-- Sarah has TWO open orders. This is deliberate: it forces the agent to ask a
-- clarifying question instead of guessing which order "where is my order" means.
INSERT INTO orders (order_number, customer_id, status, placed_at, shipped_at, delivered_at, carrier, tracking_no, eta, ship_zip, total_cents) VALUES
  ('BK-10021', 1, 'in_transit', CURRENT_DATE - 5,  CURRENT_DATE - 3,  NULL,             'UPS',   '1Z999AA10123456784', CURRENT_DATE + 2, '94110', 4297),
  ('BK-10044', 1, 'delivered',  CURRENT_DATE - 11, CURRENT_DATE - 8,  CURRENT_DATE - 4, 'USPS',  '9400111899223197428490', CURRENT_DATE - 4, '94110', 2899),
  -- Marcus: delivered well outside the 30-day window -> the agent must say no.
  ('BK-10102', 2, 'delivered',  CURRENT_DATE - 52, CURRENT_DATE - 49, CURRENT_DATE - 45, 'UPS',  '1Z999AA10987654321', CURRENT_DATE - 45, '02139', 6150),
  -- Priya: ETA blew past with no delivery scan -> nothing the agent can fix
  -- with a tool, so it should hand off rather than improvise.
  ('BK-10077', 3, 'delayed',    CURRENT_DATE - 21, CURRENT_DATE - 18, NULL,             'FedEx', '7712 3456 7890',     CURRENT_DATE - 6, '60614', 3450);

INSERT INTO order_items (order_id, title, author, qty, price_cents) VALUES
  (1, 'The Overstory',                   'Richard Powers',     1, 1899),
  (1, 'Piranesi',                        'Susanna Clarke',     1, 2398),
  (2, 'Tomorrow, and Tomorrow, and Tomorrow', 'Gabrielle Zevin', 1, 2899),
  (3, 'The Bee Sting',                   'Paul Murray',        1, 3200),
  (3, 'Trust',                           'Hernan Diaz',        1, 2950),
  (4, 'Sea of Tranquility',              'Emily St. John Mandel', 1, 3450);

INSERT INTO kb_articles (slug, title, body, tags) VALUES
  ('returns-policy', 'Returns & refunds',
   'Bookly accepts returns within 30 days of delivery for any reason. Books must be in resalable condition. Items that arrived damaged or defective can be returned within 90 days of delivery and ship back free. Refunds are issued to the original payment method and take 5-7 business days to appear after we receive the item. Gift cards, digital downloads and clearance titles marked FINAL SALE are not returnable.',
   'return refund damaged rma money back exchange'),
  ('shipping-times', 'Shipping times & costs',
   'Standard shipping is 3-7 business days and is free on orders over $35, otherwise $4.99. Expedited shipping is 2 business days for $9.99. Orders placed after 2pm ET ship the next business day. We currently ship to the United States and Canada only. Tracking information is emailed when the carrier scans the parcel.',
   'shipping delivery cost how long free postage international'),
  ('lost-package', 'Missing or late packages',
   'If tracking has not updated for 7 days, or the delivery date has passed by more than 3 days, we treat the parcel as potentially lost and open a carrier trace. Traces take 3-5 business days. If the carrier cannot locate the parcel we reship or refund at your choice.',
   'lost missing late delayed stuck tracking not moving'),
  ('password-reset', 'Resetting your password',
   'Go to bookly.example/account/reset and enter the email on your account. The reset link is valid for 60 minutes. If it does not arrive within 10 minutes check your spam folder. For security, Bookly support can never read, set or confirm your password over chat.',
   'password login reset locked out cannot sign in account access'),
  ('order-changes', 'Changing or cancelling an order',
   'Orders can be cancelled or have their shipping address changed until they enter the packing stage, usually within 60 minutes of being placed. After a parcel ships, the address cannot be changed; you can refuse delivery or return it once it arrives.',
   'cancel change address modify edit order stop');
