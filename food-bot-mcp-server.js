#!/usr/bin/env node

/**
 * MCP Server for Food Delivery App
 * Exposes menu items (dummy data) and fetches orders and user profiles from Supabase
 * 
 * Run with: node mcp-server.js
 * Or use stdio transport for MCP clients
 * 
 * Requires environment variables:
 * - NEXT_PUBLIC_SUPABASE_URL
 * - NEXT_PUBLIC_SUPABASE_ANON_KEY
 */

import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  ListResourcesRequestSchema,
  ReadResourceRequestSchema,
} from '@modelcontextprotocol/sdk/types.js';
import { createClient } from '@supabase/supabase-js';

// Menu items (static data - could be moved to database later)
const menuItems = [
  { id: 1, name: 'Margherita Pizza', price: 12.99, category: 'Pizza', description: 'Classic pizza with tomato sauce, mozzarella, and fresh basil', available: true },
  { id: 2, name: 'Pepperoni Pizza', price: 14.99, category: 'Pizza', description: 'Pizza topped with pepperoni and mozzarella cheese', available: true },
  { id: 3, name: 'Veggie Supreme Pizza', price: 15.99, category: 'Pizza', description: 'Loaded with bell peppers, mushrooms, onions, and olives', available: true },
  { id: 4, name: 'Chicken Burger', price: 9.99, category: 'Burgers', description: 'Grilled chicken breast with lettuce, tomato, and special sauce', available: true },
  { id: 5, name: 'Beef Burger', price: 10.99, category: 'Burgers', description: 'Juicy beef patty with cheese, pickles, and onions', available: true },
  { id: 6, name: 'Vegan Burger', price: 11.99, category: 'Burgers', description: 'Plant-based patty with vegan cheese and fresh vegetables', available: true },
  { id: 7, name: 'Caesar Salad', price: 8.99, category: 'Salads', description: 'Romaine lettuce with Caesar dressing, croutons, and parmesan', available: true },
  { id: 8, name: 'Greek Salad', price: 9.99, category: 'Salads', description: 'Mixed greens with feta cheese, olives, and Greek dressing', available: true },
  { id: 9, name: 'French Fries', price: 4.99, category: 'Sides', description: 'Crispy golden fries served hot', available: true },
  { id: 10, name: 'Onion Rings', price: 5.99, category: 'Sides', description: 'Battered and fried onion rings', available: true },
  { id: 11, name: 'Soda', price: 2.99, category: 'Drinks', description: 'Assorted soft drinks', available: true },
  { id: 12, name: 'Bottled Water', price: 1.99, category: 'Drinks', description: 'Purified bottled water', available: true },
];

// Initialize Supabase client
const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

let supabase = null;
if (supabaseUrl && supabaseAnonKey) {
  supabase = createClient(supabaseUrl, supabaseAnonKey);
} else {
  console.error('Warning: Supabase environment variables not set. Orders and user profiles will not be available.');
}

// Helper function to format order from database
function formatOrder(order) {
  return {
    id: order.id,
    userId: order.user_id,
    items: order.items,
    totalAmount: parseFloat(order.total_amount),
    status: order.status,
    address: order.address,
    specialInstructions: order.special_instructions || '',
    createdAt: order.created_at,
    estimatedDelivery: order.estimated_delivery,
    deliveredAt: order.delivered_at || null,
  };
}

// Helper function to format user profile from database
function formatUserProfile(profile) {
  return {
    id: profile.id,
    name: profile.name,
    email: profile.email,
    preferredCuisine: profile.preferred_cuisine,
    dietaryRestrictions: profile.dietary_restrictions,
    membershipLevel: profile.membership_level,
    createdAt: profile.created_at,
    updatedAt: profile.updated_at,
  };
}

// Create MCP server
const server = new Server(
  {
    name: 'food-delivery-mcp-server',
    version: '1.0.0',
  },
  {
    capabilities: {
      tools: {},
      resources: {},
    },
  }
);

// List available tools
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: 'get_menu_items',
        description: 'Get all available menu items, optionally filtered by category',
        inputSchema: {
          type: 'object',
          properties: {
            category: {
              type: 'string',
              description: 'Filter menu items by category (Pizza, Burgers, Salads, Sides, Drinks). Leave empty to get all items.',
              enum: ['Pizza', 'Burgers', 'Salads', 'Sides', 'Drinks'],
            },
          },
        },
      },
      {
        name: 'get_order_status',
        description: 'Get the status and details of a specific order by order ID',
        inputSchema: {
          type: 'object',
          properties: {
            orderId: {
              type: 'string',
              description: 'The order ID to look up',
            },
          },
          required: ['orderId'],
        },
      },
      {
        name: 'get_user_orders',
        description: 'Get all orders for a specific user, optionally filtered by status',
        inputSchema: {
          type: 'object',
          properties: {
            userId: {
              type: 'string',
              description: 'The user ID to get orders for',
            },
            status: {
              type: 'string',
              description: 'Filter orders by status (pending, confirmed, preparing, out_for_delivery, delivered, cancelled)',
              enum: ['pending', 'confirmed', 'preparing', 'out_for_delivery', 'delivered', 'cancelled'],
            },
            limit: {
              type: 'number',
              description: 'Maximum number of orders to return (default: 10)',
            },
          },
          required: ['userId'],
        },
      },
      {
        name: 'create_order',
        description: 'Create a new food delivery order',
        inputSchema: {
          type: 'object',
          properties: {
            userId: {
              type: 'string',
              description: 'The user ID placing the order',
            },
            items: {
              type: 'array',
              description: 'Array of items to order',
              items: {
                type: 'object',
                properties: {
                  id: { type: 'number', description: 'Menu item ID' },
                  quantity: { type: 'number', description: 'Quantity of the item' },
                },
                required: ['id', 'quantity'],
              },
            },
            address: {
              type: 'string',
              description: 'Delivery address',
            },
            specialInstructions: {
              type: 'string',
              description: 'Special delivery instructions (optional)',
            },
          },
          required: ['userId', 'items', 'address'],
        },
      },
      {
        name: 'get_user_profile',
        description: 'Get user profile information by user ID',
        inputSchema: {
          type: 'object',
          properties: {
            userId: {
              type: 'string',
              description: 'The user ID to get profile for',
            },
          },
          required: ['userId'],
        },
      },
    ],
  };
});

// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    switch (name) {
      case 'get_menu_items': {
        const category = args?.category;
        let items = menuItems;
        
        if (category) {
          items = items.filter(item => item.category === category);
        }
        
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                items,
                total: items.length,
                category: category || 'all',
              }, null, 2),
            },
          ],
        };
      }

      case 'get_order_status': {
        const orderId = args?.orderId;
        if (!orderId) {
          throw new Error('Order ID is required');
        }
        
        if (!supabase) {
          throw new Error('Supabase is not configured. Please set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY environment variables.');
        }
        
        const { data: order, error } = await supabase
          .from('orders')
          .select('*')
          .eq('id', orderId)
          .single();
        
        if (error || !order) {
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify({
                  error: 'Order not found',
                  orderId,
                }, null, 2),
              },
            ],
            isError: true,
          };
        }
        
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(formatOrder(order), null, 2),
            },
          ],
        };
      }

      case 'get_user_orders': {
        const userId = args?.userId;
        const status = args?.status;
        const limit = args?.limit || 10;
        
        if (!userId) {
          throw new Error('User ID is required');
        }
        
        if (!supabase) {
          throw new Error('Supabase is not configured. Please set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY environment variables.');
        }
        
        let query = supabase
          .from('orders')
          .select('*')
          .eq('user_id', userId)
          .order('created_at', { ascending: false })
          .limit(limit);
        
        if (status) {
          query = query.eq('status', status);
        }
        
        const { data: orders, error } = await query;
        
        if (error) {
          throw new Error(`Failed to fetch orders: ${error.message}`);
        }
        
        const formattedOrders = (orders || []).map(formatOrder);
        
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                orders: formattedOrders,
                total: formattedOrders.length,
                userId,
                status: status || 'all',
              }, null, 2),
            },
          ],
        };
      }

      case 'create_order': {
        const { userId, items, address, specialInstructions } = args;
        
        if (!userId || !items || !address) {
          throw new Error('userId, items, and address are required');
        }
        
        if (!supabase) {
          throw new Error('Supabase is not configured. Please set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY environment variables.');
        }
        
        // Ensure user profile exists
        const { data: existingProfile } = await supabase
          .from('user_profiles')
          .select('id')
          .eq('id', userId)
          .single();
        
        if (!existingProfile) {
          // Create default profile if it doesn't exist
          const { error: profileError } = await supabase
            .from('user_profiles')
            .insert({
              id: userId,
              name: `User_${userId.substring(0, 8)}`,
              preferred_cuisine: null,
              dietary_restrictions: null,
              membership_level: 'standard',
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            });
          
          if (profileError && profileError.code !== '23505') { // Ignore duplicate key errors
            console.error('Error creating user profile:', profileError);
          }
        }
        
        // Validate items
        const validItems = [];
        let totalAmount = 0;
        
        for (const item of items) {
          const menuItem = menuItems.find(mi => mi.id === item.id);
          if (!menuItem) {
            throw new Error(`Menu item with ID ${item.id} not found`);
          }
          if (!menuItem.available) {
            throw new Error(`Menu item ${menuItem.name} is not available`);
          }
          
          const quantity = item.quantity || 1;
          validItems.push({
            id: menuItem.id,
            name: menuItem.name,
            price: menuItem.price,
            quantity,
          });
          
          totalAmount += menuItem.price * quantity;
        }
        
        // Create order in Supabase
        const orderData = {
          user_id: userId,
          items: validItems,
          total_amount: Math.round(totalAmount * 100) / 100, // Round to 2 decimal places
          status: 'confirmed',
          address,
          special_instructions: specialInstructions || '',
          created_at: new Date().toISOString(),
          estimated_delivery: new Date(Date.now() + (20 + Math.random() * 20) * 60000).toISOString(), // 20-40 minutes
        };
        
        const { data: savedOrder, error: orderError } = await supabase
          .from('orders')
          .insert(orderData)
          .select()
          .single();
        
        if (orderError || !savedOrder) {
          throw new Error(`Failed to create order: ${orderError?.message || 'Unknown error'}`);
        }
        
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify({
                success: true,
                order: formatOrder(savedOrder),
              }, null, 2),
            },
          ],
        };
      }

      case 'get_user_profile': {
        const userId = args?.userId;
        
        if (!userId) {
          throw new Error('User ID is required');
        }
        
        if (!supabase) {
          throw new Error('Supabase is not configured. Please set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY environment variables.');
        }
        
        const { data: profile, error } = await supabase
          .from('user_profiles')
          .select('*')
          .eq('id', userId)
          .single();
        
        if (error || !profile) {
          return {
            content: [
              {
                type: 'text',
                text: JSON.stringify({
                  error: 'User profile not found',
                  userId,
                }, null, 2),
              },
            ],
            isError: true,
          };
        }
        
        return {
          content: [
            {
              type: 'text',
              text: JSON.stringify(formatUserProfile(profile), null, 2),
            },
          ],
        };
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (error) {
    return {
      content: [
        {
          type: 'text',
          text: JSON.stringify({
            error: error.message || 'An error occurred',
            tool: name,
          }, null, 2),
        },
      ],
      isError: true,
    };
  }
});

// List available resources
server.setRequestHandler(ListResourcesRequestSchema, async () => {
  return {
    resources: [
      {
        uri: 'food-delivery://menu',
        name: 'Menu Items',
        description: 'Complete menu with all available food items',
        mimeType: 'application/json',
      },
      {
        uri: 'food-delivery://orders',
        name: 'All Orders',
        description: 'List of all orders in the system',
        mimeType: 'application/json',
      },
      {
        uri: 'food-delivery://users',
        name: 'User Profiles',
        description: 'List of all user profiles',
        mimeType: 'application/json',
      },
    ],
  };
});

// Handle resource reads
server.setRequestHandler(ReadResourceRequestSchema, async (request) => {
  const { uri } = request.params;

  try {
    switch (uri) {
      case 'food-delivery://menu':
        return {
          contents: [
            {
              uri,
              mimeType: 'application/json',
              text: JSON.stringify({
                menuItems,
                categories: [...new Set(menuItems.map(item => item.category))],
                totalItems: menuItems.length,
              }, null, 2),
            },
          ],
        };

      case 'food-delivery://orders':
        if (!supabase) {
          throw new Error('Supabase is not configured. Please set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY environment variables.');
        }
        
        const { data: orders, error: ordersError } = await supabase
          .from('orders')
          .select('*')
          .order('created_at', { ascending: false });
        
        if (ordersError) {
          throw new Error(`Failed to fetch orders: ${ordersError.message}`);
        }
        
        const formattedOrders = (orders || []).map(formatOrder);
        const statusCounts = formattedOrders.reduce((acc, order) => {
          acc[order.status] = (acc[order.status] || 0) + 1;
          return acc;
        }, {});
        
        return {
          contents: [
            {
              uri,
              mimeType: 'application/json',
              text: JSON.stringify({
                orders: formattedOrders,
                totalOrders: formattedOrders.length,
                statusCounts,
              }, null, 2),
            },
          ],
        };

      case 'food-delivery://users':
        if (!supabase) {
          throw new Error('Supabase is not configured. Please set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY environment variables.');
        }
        
        const { data: userProfiles, error: usersError } = await supabase
          .from('user_profiles')
          .select('*')
          .order('created_at', { ascending: false });
        
        if (usersError) {
          throw new Error(`Failed to fetch user profiles: ${usersError.message}`);
        }
        
        const formattedProfiles = (userProfiles || []).map(formatUserProfile);
        
        return {
          contents: [
            {
              uri,
              mimeType: 'application/json',
              text: JSON.stringify({
                userProfiles: formattedProfiles,
                totalUsers: formattedProfiles.length,
              }, null, 2),
            },
          ],
        };

      default:
        throw new Error(`Unknown resource: ${uri}`);
    }
  } catch (error) {
    return {
      contents: [
        {
          uri,
          mimeType: 'text/plain',
          text: `Error: ${error.message}`,
        },
      ],
    };
  }
});

// Start the server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  console.error('Food Delivery MCP Server running on stdio');
}

main().catch((error) => {
  console.error('Fatal error in main():', error);
  process.exit(1);
});


