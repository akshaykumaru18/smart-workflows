CREATE OR REPLACE VIEW dashboard_aggregated_summary AS
SELECT 
    COUNT(DISTINCT c.customer_id) AS total_customers,
    COUNT(DISTINCT o.order_id) AS total_orders,
    COALESCE(SUM(o.total_amount), 0) AS total_revenue,
    COALESCE(AVG(o.total_amount), 0) AS average_order_value
FROM customers c
LEFT JOIN sales_orders o ON c.customer_id = o.customer_id;