# 示例：官方 e_commerce_33 的工具清单

官方场景 `e_commerce_33` 经 env-manager 在隔离的会话目录中启动后，MCP `list_tools` 返回 39 个工具，与离线目录（`workbench env search e_commerce_33`，按 `full_code` 中的 `operation_id` 计数）一致。下表由各工具的 `inputSchema` 整理，只列工具名与参数名，不含描述、代码或数据行。

迷你夹具 `tests/fixtures/awm_mini` 的 7 个工具取自这张表：`search_products`、`get_product_by_id`、`list_cart_items`、`add_item_to_cart`、`remove_cart_item`、`list_user_payment_methods`、`delete_user_payment_method`。夹具工具的必填字段与官方完全相同，参数名是官方参数的子集；有官方数据时，`tests/integration/test_official_data.py` 会真实启动两个环境并核对这一点。

来源：AgentWorldModel-1K（`Snowflake/AgentWorldModel-1K`，revision `dde80a0283fe781bdc51656bce57063dc5650213`），作者 Zhaoyang Wang, Canwen Xu, Boyi Liu, Yite Wang, Siwei Han, Zhewei Yao, Huaxiu Yao, Yuxiong He；许可证 [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)。改动：只摘录工具名与参数名。

| # | 工具 | 必填参数 | 全部参数 |
|---|---|---|---|
| 1 | `search_products` | — | `category_id`, `is_prime_eligible`, `limit`, `max_price`, `min_average_rating`, `offset`, `query`, `sort_by` |
| 2 | `get_product_by_id` | `product_id` | `product_id` |
| 3 | `list_product_categories` | — | `parent_id` |
| 4 | `list_product_versions` | `product_id` | `format`, `is_digital`, `product_id`, `version_type` |
| 5 | `list_product_reviews` | `product_id` | `limit`, `min_rating`, `offset`, `product_id` |
| 6 | `create_product_review` | `product_id`, `rating`, `title` | `body`, `product_id`, `rating`, `title` |
| 7 | `list_product_offers` | `product_id` | `is_prime_eligible`, `max_price`, `product_id`, `sort_by` |
| 8 | `get_top_products` | — | `category_id`, `limit`, `max_price`, `min_average_rating`, `sort_by` |
| 9 | `get_or_create_active_cart` | — | — |
| 10 | `list_cart_items` | — | — |
| 11 | `add_item_to_cart` | `product_offer_id`, `quantity` | `merge_if_exists`, `product_offer_id`, `quantity` |
| 12 | `update_cart_item_quantity` | `cart_item_id`, `quantity` | `cart_item_id`, `quantity` |
| 13 | `remove_cart_item` | `cart_item_id` | `cart_item_id` |
| 14 | `list_orders` | — | `limit`, `offset`, `placed_after`, `placed_before`, `status` |
| 15 | `get_order_by_id` | `order_id` | `order_id` |
| 16 | `list_order_items` | `order_id` | `order_id` |
| 17 | `search_order_items_by_title` | `query` | `limit`, `offset`, `placed_after`, `placed_before`, `query`, `sort_by` |
| 18 | `create_return_request` | `order_id`, `order_item_id`, `reason_code`, `refund_method` | `order_id`, `order_item_id`, `reason_code`, `refund_method` |
| 19 | `preview_checkout_for_active_cart` | — | — |
| 20 | `purchase_single_digital_product` | `product_id` | `max_price`, `product_id`, `product_version_id` |
| 21 | `submit_order_from_active_cart` | — | — |
| 22 | `list_user_addresses` | — | — |
| 23 | `create_user_address` | `city`, `line1`, `postal_code`, `state` | `city`, `country`, `label`, `line1`, `line2`, `postal_code`, `set_as_default_shipping`, `state` |
| 24 | `update_user_address` | `address_id` | `address_id`, `city`, `country`, `label`, `line1`, `line2`, `postal_code`, `set_as_default_shipping`, `state` |
| 25 | `set_default_shipping_address` | `address_id` | `address_id` |
| 26 | `list_user_payment_methods` | — | — |
| 27 | `create_user_payment_method` | `payment_type` | `card_brand`, `card_exp_month`, `card_exp_year`, `card_number`, `cvv`, `payment_type`, `set_as_default` |
| 28 | `set_default_payment_method` | `payment_method_id` | `payment_method_id` |
| 29 | `delete_user_payment_method` | `payment_method_id` | `payment_method_id` |
| 30 | `list_user_devices` | — | — |
| 31 | `list_user_wishlists` | — | — |
| 32 | `create_user_wishlist` | `name` | `is_default`, `name` |
| 33 | `list_wishlist_items` | `wishlist_id` | `wishlist_id` |
| 34 | `add_item_to_wishlist` | `product_id`, `wishlist_id` | `product_id`, `wishlist_id` |
| 35 | `remove_item_from_wishlist` | `wishlist_id`, `wishlist_item_id` | `wishlist_id`, `wishlist_item_id` |
| 36 | `list_product_subscription_offers` | `product_id` | `product_id` |
| 37 | `list_user_subscriptions` | — | `status` |
| 38 | `create_user_subscription` | `cadence_months`, `product_id`, `quantity` | `cadence_months`, `payment_method_id`, `product_id`, `quantity`, `shipping_address_id`, `subscription_offer_id` |
| 39 | `update_user_subscription` | `subscription_id` | `cadence_months`, `next_delivery_date`, `payment_method_id`, `quantity`, `shipping_address_id`, `status`, `subscription_id` |
