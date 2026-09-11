<!-- include start from serial/service/modbus-not-profileable.xml.i -->
<node name="modbus-gateway">
  <children>
    <node name="slave">
      <properties>
        <help>Modbus gateway slave settings</help>
      </properties>
      <children>
        #include <include/serial/service/utils/aliasing-address.xml.i>
        <leafNode name="uid">
          <properties>
            <help>Slave UID or UID range</help>
            <valueHelp>
              <format>start-end</format>
              <description>UID range (e.g. 2-5) to match</description>
            </valueHelp>
            <valueHelp>
              <format>&lt;1-247&gt;</format>
              <description>UID number, from 1 to 247</description>
            </valueHelp>
            <constraint>
              <validator name="modbus-uid-range"/>
            </constraint>
          </properties>
        </leafNode>
        <node name="remap-uid-list">
          <properties>
            <help>Source master remapping list</help>
          </properties>
          <children>
            <tagNode name="entry">
              <properties>
                <help>Slave remap list entry</help>
                <valueHelp>
                  <format>u32:1-48</format>
                  <description>Mapping Entry ID (1-48)</description>
                </valueHelp>
                <constraint>
                  <validator name="numeric" argument="--range 1-48"/>
                </constraint>
              </properties>
              <children>
                <leafNode name="from">
                  <properties>
                    <help>Source master UID or UID range to remap from</help>
                    <valueHelp>
                      <format>start-end</format>
                      <description>UID range (e.g. 2-5) to match</description>
                    </valueHelp>
                    <valueHelp>
                      <format>&lt;1-247&gt;</format>
                      <description>UID number, from 1 to 247</description>
                    </valueHelp>
                    <constraint>
                      <validator name="modbus-uid-range"/>
                    </constraint>
                  </properties>
                </leafNode>
                <leafNode name="to">
                  <properties>
                    <help>Destination slave UID or UID range to remap to</help>
                    <valueHelp>
                      <format>start-end</format>
                      <description>UID range (e.g. 2-5) to match</description>
                    </valueHelp>
                    <valueHelp>
                      <format>&lt;1-247&gt;</format>
                      <description>UID number, from 1 to 247</description>
                    </valueHelp>
                    <constraint>
                      <validator name="modbus-uid-range"/>
                    </constraint>
                  </properties>
                </leafNode>
              </children>
            </tagNode>
          </children>
        </node>
      </children>
    </node>
  </children>
</node>
<!-- include end -->
