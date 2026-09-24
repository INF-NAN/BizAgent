# HAND-WRITTEN FIXTURE — NOT official AgentWorldModel-1K data.
#
# A tiny FastAPI app written in the same shape as AWM-generated environment code
# (DATABASE_PATH env var, one-line create_engine, operation_id per route, uvicorn entry
# point at the end) so that AWM's real launcher (awm.core.server) can patch and serve it
# over MCP Streamable HTTP. Used only for offline CPU tests and the mock demo.
#
# Interface provenance (reconciled on 2026-09-24, docs/verification/2026-09-24-fixture-reconciliation.md):
# the 7 tool names, their parameter names / required-ness and the top-level response field
# names follow the official `e_commerce_33` environment of AgentWorldModel-1K
# (Snowflake/AgentWorldModel-1K @ dde80a0, CC-BY-4.0, by Zhaoyang Wang et al.). The code
# below is written from scratch and heavily simplified (fewer parameters, tables and fields);
# no official code or data rows are copied.
import os
from datetime import datetime
from typing import List, Optional

from fastapi import Body, FastAPI, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_PATH") or "sqlite:///mini_e_commerce.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(String)
    is_prime_eligible = Column(Integer, nullable=False)


class ProductAggregate(Base):
    __tablename__ = "product_aggregates"
    product_id = Column(Integer, ForeignKey("products.id"), primary_key=True)
    average_rating = Column(Float, nullable=False)


class ProductOffer(Base):
    __tablename__ = "product_offers"
    id = Column(Integer, primary_key=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String, nullable=False)
    is_active = Column(Integer, nullable=False)


class Cart(Base):
    __tablename__ = "carts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    status = Column(String, nullable=False)


class CartItem(Base):
    __tablename__ = "cart_items"
    id = Column(Integer, primary_key=True)
    cart_id = Column(Integer, ForeignKey("carts.id"), nullable=False)
    product_offer_id = Column(Integer, ForeignKey("product_offers.id"), nullable=False)
    quantity = Column(Integer, nullable=False)


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    payment_type = Column(String, nullable=False)
    card_brand = Column(String)
    card_last4 = Column(String)
    is_default = Column(Integer, nullable=False)


Base.metadata.create_all(engine)

app = FastAPI(title="mini_e_commerce")


class ProductModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(..., description="Product ID.", examples=[1])
    title: str = Field(..., description="Product title.", examples=["Headphones"])
    description: Optional[str] = Field(None, description="Product description.", examples=["Over-ear"])
    is_prime_eligible: bool = Field(..., description="Whether the product is Prime-eligible.", examples=[True])


class AggregateModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    product_id: int = Field(..., description="Product ID.", examples=[1])
    average_rating: float = Field(..., description="Average review rating.", examples=[4.5])


class OfferModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(..., description="Offer ID (use this as product_offer_id).", examples=[11])
    product_id: int = Field(..., description="Product ID.", examples=[1])
    price: float = Field(..., description="Offer price.", examples=[99.0])
    currency: str = Field(..., description="Currency code.", examples=["USD"])


class SearchItemModel(BaseModel):
    product: ProductModel = Field(..., description="Product info.")
    aggregates: Optional[AggregateModel] = Field(None, description="Aggregate metrics for the product.")
    lowest_active_offer: Optional[OfferModel] = Field(None, description="Cheapest active offer, if any.")


class SearchResponseModel(BaseModel):
    products: List[SearchItemModel] = Field(..., description="Matching products with pricing info.")
    total: int = Field(..., description="Number of matched products.", examples=[2])


class ProductDetailResponseModel(BaseModel):
    product: ProductModel = Field(..., description="Product info.")
    aggregates: Optional[AggregateModel] = Field(None, description="Aggregate metrics for the product.")
    active_offers: List[OfferModel] = Field(..., description="Active offers for the product.")


class CartItemWithProductModel(BaseModel):
    id: int = Field(..., description="Cart item ID.", examples=[1])
    product_offer_id: int = Field(..., description="Offer ID for the item.", examples=[11])
    quantity: int = Field(..., description="Quantity in the cart.", examples=[1])
    product_id: int = Field(..., description="Product ID of the offer.", examples=[1])
    product_title: str = Field(..., description="Product title.", examples=["Headphones"])
    price: float = Field(..., description="Unit price of the offer.", examples=[99.0])
    currency: str = Field(..., description="Currency code.", examples=["USD"])


class CartItemsListResponseModel(BaseModel):
    cart_id: int = Field(..., description="Active cart ID.", examples=[1])
    items: List[CartItemWithProductModel] = Field(..., description="Items in the active cart.")


class CartItemCreateBodyModel(BaseModel):
    product_offer_id: int = Field(..., description="ID of the product_offers row to add.", examples=[11])
    quantity: int = Field(..., description="Quantity to add (must be >0).", examples=[1])
    merge_if_exists: Optional[bool] = Field(False, description="If true, increase quantity if already in cart.")


class CartItemModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(..., description="Cart item ID.", examples=[2])
    cart_id: int = Field(..., description="Cart ID.", examples=[1])
    product_offer_id: int = Field(..., description="Offer ID.", examples=[11])
    quantity: int = Field(..., description="Final quantity in cart.", examples=[1])


class CartItemCreateResponseModel(BaseModel):
    cart_item: CartItemModel = Field(..., description="Created or updated cart item.")


class PaymentMethodModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(..., description="Payment method ID.", examples=[1])
    payment_type: str = Field(..., description="Type such as 'credit_card'.", examples=["credit_card"])
    card_brand: Optional[str] = Field(None, description="Card brand.", examples=["Visa"])
    card_last4: Optional[str] = Field(None, description="Last 4 digits.", examples=["4242"])
    is_default: bool = Field(..., description="Whether this is the default method.", examples=[True])


class PaymentMethodsListResponseModel(BaseModel):
    payment_methods: List[PaymentMethodModel] = Field(..., description="Saved payment methods.")


class SuccessResponseModel(BaseModel):
    success: bool = Field(..., description="Whether the operation succeeded.", examples=[True])


def _active_cart(session):
    cart = session.query(Cart).filter(Cart.user_id == 1, Cart.status == "active").first()
    if cart is None:
        cart = Cart(user_id=1, status="active")
        session.add(cart)
        session.commit()
        session.refresh(cart)
    return cart


@app.get("/api/products/search", response_model=SearchResponseModel, summary="Search products",
         description="Search products by text with an optional price cap and sort order.",
         tags=["products"], operation_id="search_products")
async def search_products(
    query: Optional[str] = Query(None, description="Search on product title and description."),
    max_price: Optional[float] = Query(None, description="Maximum offer price (lowest active offer)."),
    sort_by: Optional[str] = Query(None, description="Sort key: 'relevance','average_rating','price_asc','price_desc'."),
    limit: Optional[int] = Query(20, description="Max number of products to return."),
) -> SearchResponseModel:
    session = SessionLocal()
    q = session.query(Product)
    if query is not None:
        like = f"%{query}%"
        q = q.filter(Product.title.ilike(like) | Product.description.ilike(like))
    items = []
    for p in q.order_by(Product.id).all():
        offers = session.query(ProductOffer).filter(ProductOffer.product_id == p.id, ProductOffer.is_active == 1)
        if max_price is not None:
            offers = offers.filter(ProductOffer.price <= max_price)
        lowest = offers.order_by(ProductOffer.price.asc()).first()
        if max_price is not None and lowest is None:
            continue
        agg = session.query(ProductAggregate).filter(ProductAggregate.product_id == p.id).first()
        items.append(SearchItemModel(
            product=ProductModel(id=p.id, title=p.title, description=p.description,
                                 is_prime_eligible=bool(p.is_prime_eligible)),
            aggregates=AggregateModel.model_validate(agg) if agg else None,
            lowest_active_offer=OfferModel.model_validate(lowest) if lowest else None,
        ))
    if sort_by == "average_rating":
        items.sort(key=lambda i: -(i.aggregates.average_rating if i.aggregates else 0.0))
    elif sort_by in ("price_asc", "price_desc"):
        priced = [i for i in items if i.lowest_active_offer]
        priced.sort(key=lambda i: i.lowest_active_offer.price, reverse=sort_by == "price_desc")
        items = priced + [i for i in items if not i.lowest_active_offer]
    session.close()
    return SearchResponseModel(products=items[: limit or 20], total=len(items))


@app.get("/api/products/{product_id}", response_model=ProductDetailResponseModel, summary="Get product",
         description="Get a product with aggregates and active offers.", tags=["products"],
         operation_id="get_product_by_id")
async def get_product_by_id(product_id: int = Path(..., description="ID of the product to retrieve.")):
    session = SessionLocal()
    p = session.query(Product).filter(Product.id == product_id).first()
    agg = session.query(ProductAggregate).filter(ProductAggregate.product_id == product_id).first()
    offers = session.query(ProductOffer).filter(ProductOffer.product_id == product_id, ProductOffer.is_active == 1).all()
    out = ProductDetailResponseModel(
        product=ProductModel(id=p.id, title=p.title, description=p.description, is_prime_eligible=bool(p.is_prime_eligible)),
        aggregates=AggregateModel.model_validate(agg) if agg else None,
        active_offers=[OfferModel.model_validate(o) for o in offers],
    )
    session.close()
    return out


@app.get("/api/cart/items", response_model=CartItemsListResponseModel, summary="List cart items",
         description="List items in the active cart of user_id=1 with offer details.", tags=["cart"],
         operation_id="list_cart_items")
async def list_cart_items() -> CartItemsListResponseModel:
    session = SessionLocal()
    cart = _active_cart(session)
    rows = []
    for ci in session.query(CartItem).filter(CartItem.cart_id == cart.id).all():
        offer = session.query(ProductOffer).filter(ProductOffer.id == ci.product_offer_id).first()
        product = session.query(Product).filter(Product.id == offer.product_id).first() if offer else None
        rows.append(CartItemWithProductModel(
            id=ci.id, product_offer_id=ci.product_offer_id, quantity=ci.quantity,
            product_id=offer.product_id if offer else 0, product_title=product.title if product else "",
            price=offer.price if offer else 0.0, currency=offer.currency if offer else "USD",
        ))
    out = CartItemsListResponseModel(cart_id=cart.id, items=rows)
    session.close()
    return out


@app.post("/api/cart/items", response_model=CartItemCreateResponseModel, summary="Add item to cart",
          description="Add a product offer to the active cart of user_id=1.", tags=["cart"],
          operation_id="add_item_to_cart")
async def add_item_to_cart(body: CartItemCreateBodyModel = Body(...)) -> CartItemCreateResponseModel:
    session = SessionLocal()
    cart = _active_cart(session)
    item = session.query(CartItem).filter(CartItem.cart_id == cart.id,
                                          CartItem.product_offer_id == body.product_offer_id).first()
    if item is not None and body.merge_if_exists:
        item.quantity = item.quantity + body.quantity
    elif item is not None:
        item.quantity = body.quantity
    else:
        item = CartItem(cart_id=cart.id, product_offer_id=body.product_offer_id, quantity=body.quantity)
        session.add(item)
    session.commit()
    session.refresh(item)
    out = CartItemCreateResponseModel(cart_item=CartItemModel.model_validate(item))
    session.close()
    return out


@app.delete("/api/cart/items/{cart_item_id}", response_model=SuccessResponseModel, summary="Remove cart item",
            description="Remove an item from the active cart.", tags=["cart"], operation_id="remove_cart_item")
async def remove_cart_item(cart_item_id: int = Path(..., description="ID of the cart item to remove.")):
    session = SessionLocal()
    cart = _active_cart(session)
    item = session.query(CartItem).filter(CartItem.id == cart_item_id, CartItem.cart_id == cart.id).first()
    if item is None:
        session.close()
        return SuccessResponseModel(success=False)
    session.delete(item)
    session.commit()
    session.close()
    return SuccessResponseModel(success=True)


@app.get("/api/users/me/payment-methods", response_model=PaymentMethodsListResponseModel,
         summary="List payment methods", description="List saved payment methods of user_id=1.",
         tags=["payments"], operation_id="list_user_payment_methods")
async def list_user_payment_methods() -> PaymentMethodsListResponseModel:
    session = SessionLocal()
    rows = [PaymentMethodModel(id=p.id, payment_type=p.payment_type, card_brand=p.card_brand,
                               card_last4=p.card_last4, is_default=bool(p.is_default))
            for p in session.query(PaymentMethod).filter(PaymentMethod.user_id == 1).all()]
    session.close()
    return PaymentMethodsListResponseModel(payment_methods=rows)


@app.delete("/api/users/me/payment-methods/{payment_method_id}", response_model=SuccessResponseModel,
            summary="Delete payment method", description="Delete a saved payment method of user_id=1.",
            tags=["payments"], operation_id="delete_user_payment_method")
async def delete_user_payment_method(payment_method_id: int = Path(..., description="Payment method ID to delete.")):
    session = SessionLocal()
    pm = session.query(PaymentMethod).filter(PaymentMethod.id == payment_method_id, PaymentMethod.user_id == 1).first()
    if pm is None:
        session.close()
        return SuccessResponseModel(success=False)
    session.delete(pm)
    session.commit()
    session.close()
    return SuccessResponseModel(success=True)


if __name__ == "__main__":
    import uvicorn, os
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
