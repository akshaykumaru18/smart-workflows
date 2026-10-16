import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns

st.title('E-Commerce Dashboard')

@st.cache_data
def load_data():
    try:
        return pd.read_csv('orders/orders.csv')
    except:
        return pd.DataFrame({
            'order_id': range(1, 11),
            'customer': [f'Customer {i}' for i in range(1, 11)],
            'product': [f'Product {i}' for i in range(1, 11)],
            'qty': range(1, 11),
            'price': [10.0 * i for i in range(1, 11)],
            'date': pd.date_range(start='2023-10-01', periods=10)
        })

df = load_data()
st.dataframe(df)

st.subheader('Sales by Product')
fig, ax = plt.subplots()
sns.barplot(data=df, x='product', y='price', ax=ax)
st.pyplot(fig)

st.subheader('Quantity Distribution')
fig2, ax2 = plt.subplots()
sns.histplot(df['qty'], kde=True, ax=ax2)
st.pyplot(fig2)
