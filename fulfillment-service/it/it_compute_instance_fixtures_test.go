/*
Copyright (c) 2025 Red Hat Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

  http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package it

import (
	"context"
	"time"

	. "github.com/onsi/gomega"
	privatev1 "github.com/osac-project/osac/proto/gen/osac/private/v1"
	publicv1 "github.com/osac-project/osac/proto/gen/osac/public/v1"
	grpccodes "google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
)

type computeInstanceFixtureClients struct {
	subnets                  privatev1.SubnetsClient
	virtualNetworks          privatev1.VirtualNetworksClient
	networkClasses           privatev1.NetworkClassesClient
	computeInstances         publicv1.ComputeInstancesClient
	computeInstanceTemplates privatev1.ComputeInstanceTemplatesClient
	instanceTypes            privatev1.InstanceTypesClient
	storageTiers             privatev1.StorageTiersClient
	storageBackends          privatev1.StorageBackendsClient
	diskImages               privatev1.DiskImagesClient
}

const computeInstanceFixtureProbeTimeout = 10 * time.Second

func newComputeInstanceFixtureClients() computeInstanceFixtureClients {
	return computeInstanceFixtureClients{
		subnets:                  privatev1.NewSubnetsClient(tool.InternalView().AdminConn()),
		virtualNetworks:          privatev1.NewVirtualNetworksClient(tool.InternalView().AdminConn()),
		networkClasses:           privatev1.NewNetworkClassesClient(tool.InternalView().AdminConn()),
		computeInstances:         publicv1.NewComputeInstancesClient(tool.ExternalView().UserConn()),
		computeInstanceTemplates: privatev1.NewComputeInstanceTemplatesClient(tool.InternalView().AdminConn()),
		instanceTypes:            privatev1.NewInstanceTypesClient(tool.InternalView().AdminConn()),
		storageTiers:             privatev1.NewStorageTiersClient(tool.InternalView().AdminConn()),
		storageBackends:          privatev1.NewStorageBackendsClient(tool.InternalView().AdminConn()),
		diskImages:               privatev1.NewDiskImagesClient(tool.InternalView().AdminConn()),
	}
}

func waitForComputeInstanceFixtureStorageBackend(ctx context.Context, client privatev1.StorageBackendsClient, id string) {
	Eventually(func(g Gomega) {
		probeCtx, cancel := context.WithTimeout(ctx, computeInstanceFixtureProbeTimeout)
		defer cancel()
		_, err := client.Get(probeCtx, privatev1.StorageBackendsGetRequest_builder{Id: id}.Build())
		g.Expect(err).ToNot(HaveOccurred())
	}, time.Minute, time.Second).Should(Succeed())
}

func cleanupComputeInstanceFixture(
	ctx context.Context,
	clients computeInstanceFixtureClients,
	computeInstanceID, resizeInstanceTypeID, instanceTypeID, subnetID, virtualNetworkID,
	networkClassID, computeInstanceTemplateID, diskImageID, storageTierID, storageBackendID string,
) {
	if computeInstanceID != "" {
		_, err := clients.computeInstances.Delete(ctx, publicv1.ComputeInstancesDeleteRequest_builder{Id: computeInstanceID}.Build())
		expectFixtureDelete(err)
	}
	if resizeInstanceTypeID != "" {
		_, err := clients.instanceTypes.Delete(ctx, privatev1.InstanceTypesDeleteRequest_builder{Id: resizeInstanceTypeID}.Build())
		expectFixtureDelete(err)
	}
	if instanceTypeID != "" {
		_, err := clients.instanceTypes.Delete(ctx, privatev1.InstanceTypesDeleteRequest_builder{Id: instanceTypeID}.Build())
		expectFixtureDelete(err)
	}
	if subnetID != "" {
		_, err := clients.subnets.Delete(ctx, privatev1.SubnetsDeleteRequest_builder{Id: subnetID}.Build())
		expectFixtureDelete(err)
	}
	if virtualNetworkID != "" {
		_, err := clients.virtualNetworks.Delete(ctx, privatev1.VirtualNetworksDeleteRequest_builder{Id: virtualNetworkID}.Build())
		expectFixtureDelete(err)
		Eventually(func(g Gomega) {
			probeCtx, cancel := context.WithTimeout(ctx, computeInstanceFixtureProbeTimeout)
			defer cancel()
			_, getErr := clients.virtualNetworks.Get(probeCtx, privatev1.VirtualNetworksGetRequest_builder{Id: virtualNetworkID}.Build())
			g.Expect(grpcstatus.Code(getErr)).To(Equal(grpccodes.NotFound))
		}, time.Minute, time.Second).Should(Succeed())
	}
	if networkClassID != "" {
		_, err := clients.networkClasses.Delete(ctx, privatev1.NetworkClassesDeleteRequest_builder{Id: networkClassID}.Build())
		expectFixtureDelete(err)
	}
	if computeInstanceTemplateID != "" {
		_, err := clients.computeInstanceTemplates.Delete(ctx, privatev1.ComputeInstanceTemplatesDeleteRequest_builder{Id: computeInstanceTemplateID}.Build())
		expectFixtureDelete(err)
	}
	if diskImageID != "" {
		_, err := clients.diskImages.Delete(ctx, privatev1.DiskImagesDeleteRequest_builder{Id: diskImageID}.Build())
		expectFixtureDelete(err)
	}
	if storageTierID != "" {
		_, err := clients.storageTiers.Delete(ctx, privatev1.StorageTiersDeleteRequest_builder{Id: storageTierID}.Build())
		expectFixtureDelete(err)
	}
	if storageBackendID != "" {
		_, err := clients.storageBackends.Delete(ctx, privatev1.StorageBackendsDeleteRequest_builder{Id: storageBackendID}.Build())
		expectFixtureDelete(err)
	}
}

func expectFixtureDelete(err error) {
	Expect(err == nil || grpcstatus.Code(err) == grpccodes.NotFound).To(BeTrue(),
		"fixture cleanup delete failed: %v", err)
}
