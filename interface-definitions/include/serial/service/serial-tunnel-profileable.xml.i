<!-- include start from serial/service/serial-tunnel-profileable.xml.i -->
<node name="serial-tunnel">
  <properties>
    <help>Serial tunnel service settings</help>
  </properties>
  <children>
    #include <include/serial/service/utils/tls-port.xml.i>
    #include <include/serial/service/utils/keepalive.xml.i>
    <leafNode name="break-length">
      <properties>
        <help>The length of time the break condition will be asserted when received a break signal</help>
        <valueHelp>
          <format>u32:0-65535</format>
          <description>Specifies the length in milliseconds</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 0-65535"/>
        </constraint>
      </properties>
      <defaultValue>1000</defaultValue>
    </leafNode>
    <leafNode name="delay-after-break">
      <properties>
        <help>The delay between the termination of a break condition and the time data will be sent</help>
        <valueHelp>
          <format>u32:0-65535</format>
          <description>Specifies the delay in milliseconds</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 0-65535"/>
        </constraint>
      </properties>
      <defaultValue>0</defaultValue>
    </leafNode>
    <node name="client">
      <properties>
        <help>Serial tunnel client settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/remote.xml.i>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
