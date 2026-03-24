
import * as grpc from '@grpc/grpc-js';
import * as protoLoader from '@grpc/proto-loader';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';
import { dirname } from 'path';

// --- Begin Core Gemini CLI Logic Imports ---
// Note: These paths are relative and may need adjustment.
// We will reuse as much of the existing non-interactive CLI logic as possible.
import { executeCommand } from './nonInteractiveCliCommands.js';
import {
  loadConfig,
  resolveAbbreviation,
} from './config/config.js';
import {
  getGenerativeAIService,
  prepareGoogleAIRequest,
} from './services/generativeAi.js';
import {
  FatalInputError,
  isApiError,
  UserError,
} from '@google/gemini-cli-core';
// --- End Core Gemini CLI Logic Imports ---

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const PROTO_PATH = path.join(__dirname, '../../../../service.proto');
const SOCKET_PATH = '/tmp/gemini_grpc.sock';
const BIND_ADDRESS = `unix://${SOCKET_PATH}`;

const packageDefinition = protoLoader.loadSync(PROTO_PATH, {
    keepCase: true,
    longs: String,
    enums: String,
    defaults: true,
    oneofs: true
});

const protoDescriptor = grpc.loadPackageDefinition(packageDefinition) as any;
const ipcService = protoDescriptor.ipc.DataRouter;

async function sendPayload(call: any, callback: any) {
    const prompt = call.request.data.toString('utf-8');
    const clientId = call.request.client_id;
    console.log(`[gRPC Server] Received ${prompt.length} chars from ${clientId}`);

    try {
        // This logic is adapted from `nonInteractiveCli.ts`
        const config = await loadConfig();
        const generativeAiService = await getGenerativeAIService(config);
        const abortController = new AbortController();

        // Prepare the request payload
        const request = await prepareGoogleAIRequest(prompt, config);

        // Execute the request using the core Gemini service
        const result = await generativeAiService.generateContent(
          request,
          abortController,
        );

        // Assuming the first part of the first candidate is the text response
        const responseText = result.candidates[0]?.content.parts[0]?.text || '';

        callback(null, {
            success: true,
            message: responseText,
        });

    } catch (e) {
        let message = 'An unknown error occurred.';
        if (e instanceof Error) {
            message = e.message;
        }
        console.error(`[gRPC Server] Error processing prompt: ${message}`);
        callback({
            code: grpc.status.INTERNAL,
            message: message,
        }, null);
    }
}

function main() {
    const server = new grpc.Server();
    server.addService(ipcService.service, { sendPayload: sendPayload });

    if (fs.existsSync(SOCKET_PATH)) {
        fs.unlinkSync(SOCKET_PATH);
    }

    server.bindAsync(
        BIND_ADDRESS,
        grpc.ServerCredentials.createInsecure(),
        (error, port) => {
            if (error) {
                console.error(`[gRPC Server] Bind failed: ${error.message}`);
                process.exit(1);
            }
            console.log(`[gRPC Server] Gemini gRPC Server listening on ${BIND_ADDRESS}`);
            server.start();
        }
    );
}

main();
