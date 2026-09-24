# HAND-WRITTEN FIXTURE — NOT official AgentWorldModel-1K data.
#
# A tiny FastAPI app written in the same shape as AWM-generated environment code
# (DATABASE_PATH env var, one-line create_engine, operation_id per route, uvicorn entry
# point at the end) so that AWM's real launcher (awm.core.server) can patch and serve it
# over MCP Streamable HTTP. Tool names are a subset of the `e_commerce_33` tool list seen
# in the OpenEnv capture (see docs/RECON.md §1.6); they are NOT verified against the
# official dataset. Used only for offline CPU tests and the mock demo.
import os
from typing import List, Literal, Optional

from fastapi import FastAPI, Query
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
    price = Column(Float, nullable=False)
    rating = Column(Float, nullable=False)


class CartItem(Base):
    __tablename__ = "cart_items"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)


class PaymentMethod(Base):
    __tablename__ = "payment_methods"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    brand = Column(String, nullable=False)
    last4 = Column(String, nullable=False)


Base.metadata.create_all(engine)

app = FastAPI(title="mini_e_commerce")


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(..., description="Product id", examples=[1])
    title: str = Field(..., description="Product title", examples=["Headphones"])
    price: float = Field(..., description="Price in USD", examples=[99.0])
    rating: float = Field(..., description="Average rating", examples=[4.5])


class CartItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(..., description="Cart item id", examples=[1])
    product_id: int = Field(..., description="Product id", examples=[1])
    quantity: int = Field(..., description="Quantity", examples=[1])


class AddCartItemIn(BaseModel):
    product_id: int = Field(..., description="Product to add", examples=[1])
    quantity: int = Field(..., description="Quantity to add", examples=[1])


class PaymentMethodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int = Field(..., description="Payment method id", examples=[1])
    brand: str = Field(..., description="Card brand", examples=["visa"])
    last4: str = Field(..., description="Last four digits", examples=["4242"])


class DeletedOut(BaseModel):
    deleted_id: int = Field(..., description="Id of the deleted row", examples=[1])


@app.get("/api/products", response_model=List[ProductOut], summary="Search products",
         description="Search products by keyword, optionally capped by price and sorted.",
         tags=["products"], operation_id="search_products")
async def search_products(query: str = Query(..., description="Keyword"),
                          max_price: Optional[float] = Query(None, description="Max price"),
                          sort_by: Literal["price", "rating"] = Query("rating", description="Sort key")):
    session = SessionLocal()
    q = session.query(Product).filter(Product.title.like(f"%{query}%"))
    if max_price is not None:
        q = q.filter(Product.price <= max_price)
    q = q.order_by(Product.price.asc() if sort_by == "price" else Product.rating.desc())
    rows = [ProductOut.model_validate(p) for p in q.all()]
    session.close()
    return rows


@app.get("/api/products/{product_id}", response_model=ProductOut, summary="Get product",
         description="Get a single product by id.", tags=["products"], operation_id="get_product_by_id")
async def get_product_by_id(product_id: int):
    session = SessionLocal()
    row = ProductOut.model_validate(session.query(Product).filter(Product.id == product_id).first())
    session.close()
    return row


@app.get("/api/cart/items", response_model=List[CartItemOut], summary="List cart items",
         description="List items in the active cart of the current user.", tags=["cart"],
         operation_id="list_cart_items")
async def list_cart_items():
    session = SessionLocal()
    rows = [CartItemOut.model_validate(c) for c in session.query(CartItem).filter(CartItem.user_id == 1).all()]
    session.close()
    return rows


@app.post("/api/cart/items", response_model=CartItemOut, summary="Add item to cart",
          description="Add a product to the active cart of the current user.", tags=["cart"],
          operation_id="add_item_to_cart")
async def add_item_to_cart(body: AddCartItemIn):
    session = SessionLocal()
    item = CartItem(user_id=1, product_id=body.product_id, quantity=body.quantity)
    session.add(item)
    session.commit()
    out = CartItemOut.model_validate(item)
    session.close()
    return out


@app.delete("/api/cart/items/{cart_item_id}", response_model=DeletedOut, summary="Remove cart item",
            description="Remove an item from the active cart.", tags=["cart"], operation_id="remove_cart_item")
async def remove_cart_item(cart_item_id: int):
    session = SessionLocal()
    item = session.query(CartItem).filter(CartItem.id == cart_item_id, CartItem.user_id == 1).first()
    session.delete(item)
    session.commit()
    session.close()
    return DeletedOut(deleted_id=cart_item_id)


@app.get("/api/payment-methods", response_model=List[PaymentMethodOut], summary="List payment methods",
         description="List saved payment methods of the current user.", tags=["payments"],
         operation_id="list_user_payment_methods")
async def list_user_payment_methods():
    session = SessionLocal()
    rows = [PaymentMethodOut.model_validate(p)
            for p in session.query(PaymentMethod).filter(PaymentMethod.user_id == 1).all()]
    session.close()
    return rows


@app.delete("/api/payment-methods/{payment_method_id}", response_model=DeletedOut,
            summary="Delete payment method", description="Delete a saved payment method.",
            tags=["payments"], operation_id="delete_user_payment_method")
async def delete_user_payment_method(payment_method_id: int):
    session = SessionLocal()
    pm = session.query(PaymentMethod).filter(PaymentMethod.id == payment_method_id).first()
    session.delete(pm)
    session.commit()
    session.close()
    return DeletedOut(deleted_id=payment_method_id)


if __name__ == "__main__":
    import uvicorn, os
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
